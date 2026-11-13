from dataclasses import replace

import pytest

from mihari import guardrail as g
from mihari.rollout import GRACE, ONSET, SLOW_MS, Look, Request, observe


def look(errors=0, latency=0, *, canary=4, block=0):
    return Look(block, block + GRACE, 36, canary, 0, errors, 0, latency, 0, 0, 0, 0)


def test_different_signals_in_successive_windows_do_not_trigger():
    monitor = g.Guardrail()
    assert monitor.update(look(errors=4)).trigger("joint", 0.5) is None
    assert monitor.update(look(latency=4, block=1)).trigger("joint", 0.5) is None
    scores = monitor.update(look(latency=4, block=2))
    assert scores.trigger("joint", 0.5) == "latency"
    assert scores.trigger("errors", 0.5) is None


@pytest.mark.parametrize(("errors", "latency", "reason"), [
    (4, 0, "errors"), (0, 4, "latency"), (4, 4, "both")
])
def test_joint_trigger_names_the_sustained_signals(errors, latency, reason):
    monitor = g.Guardrail()
    assert monitor.update(look(errors, latency)).score("joint") is None
    assert monitor.update(look(errors, latency, block=1)).trigger("joint", 0.5) == reason


def test_late_outcome_enters_neither_metric_and_missing_breaks_both_runs():
    window = (Request(1, True, None, True, SLOW_MS + 1, 1 + GRACE + 1),) + (
        Request(1, True, None, True, SLOW_MS + 1, 1),
    ) * 3 + (Request(1, False, None, False, SLOW_MS, 1),) * 36
    observed = observe(window, 1, 1 + GRACE, 8, 4)
    assert (observed.canary_failures, observed.canary_slow, observed.control_slow) == (3, 3, 0)
    monitor = g.Guardrail()
    assert monitor.update(look(4, 4)).score("joint") is None
    held = monitor.update(observed)
    assert held.score("errors") is held.score("joint") is None
    assert monitor.update(look(4, 4, block=2)).score("joint") is None
    assert monitor.update(look(4, 4, block=3)).trigger("joint", 0.5) == "both"


def test_synthetic_faults_can_change_each_signal_independently():
    baseline = g.make_workload("healthy", 11)
    error = g.make_workload("error-only", 11)
    late = g.make_workload("latency-only", 11)
    combined = g.make_workload("combined", 11)
    assert all(sum(row.canary for row in window) * 10 == len(window) for window in baseline)
    assert error[:ONSET] == late[:ONSET] == combined[:ONSET] == baseline[:ONSET]
    assert error != baseline and late != baseline
    for windows in zip(baseline, error, late, combined, strict=True):
        for healthy, fail, slow, both in zip(*windows, strict=True):
            assert replace(fail, failed=healthy.failed) == healthy
            assert replace(slow, latency_ms=healthy.latency_ms) == healthy
            assert replace(both, latency_ms=fail.latency_ms) == fail
            assert replace(both, failed=slow.failed) == slow
            if not healthy.canary:
                assert healthy == fail == slow == both


def test_calibration_uses_joint_crossing_not_separate_five_percent_budgets(monkeypatch):
    null = [[g.Scores(look(), 0, 0)] for _ in range(18)]
    null.extend(([g.Scores(look(), 0.4, 0.0)], [g.Scores(look(), 0.0, 0.5)]))
    calls = []

    def paths(scenario, trials, *, holdout):
        calls.append((scenario, trials, holdout))
        return iter(null)

    monkeypatch.setattr(g, "trials_for", paths)
    cutoff = g.fit_cutoffs(20)
    assert calls == [(case, 20, False) for case in g.NULLS]
    assert cutoff == {"errors": 0, "joint": 0.4}
    assert sum(path[0].trigger("joint", cutoff["joint"]) is not None for path in null) == 1


def test_calibration_and_holdout_seeds_are_disjoint(monkeypatch):
    original = g.make_workload
    seen = []

    def workload(case, seed):
        seen.append(seed)
        return original(case, seed)

    monkeypatch.setattr(g, "make_workload", workload)
    for split in (False, True):
        assert len(list(g.trials_for("healthy", 3, holdout=split))) == 3
    assert seen == [0, 2, 4, 1, 3, 5]


def test_report_includes_all_fault_types_and_their_tradeoffs(capsys):
    g.run(20)
    output = capsys.readouterr().out
    assert "joint rule calibrates the union" in output
    assert all(case in output for case in (*g.NULLS, *g.FAULTS))
    assert "false rollback" in output and "95% CI" in output
    assert "not detected" in output and "charging misses" in output and "affected-window canary sends" in output
