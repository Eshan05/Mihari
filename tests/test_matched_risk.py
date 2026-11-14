import pytest

from mihari import matched_risk as m
from mihari.guardrail import Scores


def sample(*levels):
    return [0.0] * (100 - sum(count for _, count in levels)) + [
        level for level, count in levels for _ in range(count)
    ]


def test_reference_and_joint_respect_caps_and_strict_cutoff():
    points = m.select_points({
        "errors": (sample((0.8, 3)), sample((0.8, 5))),
        "joint": (sample((0.2, 2), (0.4, 1), (0.8, 3)),
                  sample((0.2, 2), (0.4, 1), (0.8, 4))),
    })
    assert points["errors"] == m.OperatingPoint(0.0, (3, 5))
    assert points["joint"] == m.OperatingPoint(0.2, (4, 5))


def test_tie_on_gap_and_worst_null_uses_stricter_cutoff():
    points = m.select_points({
        "errors": (sample((0.8, 5)), sample((0.8, 2))),
        "joint": (sample((0.8, 5)), sample((0.2, 4), (0.4, 2), (0.8, 1))),
    })
    assert points["joint"] == m.OperatingPoint(0.4, (5, 1))


def test_no_crossable_scores_and_inconsistent_sizes():
    values = ((float("-inf"), -0.1), (-0.2, -0.3))
    assert m.select_points({"errors": values, "joint": values}) == {
        rule: m.OperatingPoint(0.0, (0, 0)) for rule in m.RULES
    }
    with pytest.raises(ValueError, match="equal sizes"):
        m.select_points({"errors": values, "joint": ((0.2,), (0.1, 0.2))})


def test_fresh_disjoint_seeds_and_only_nulls_used_in_calibration(monkeypatch):
    seen = []
    visited = []

    def make(scenario, seed):
        seen.append((scenario, seed))
        return scenario, seed

    def decide(trace, onset):
        visited.append((trace, onset))
        yield Scores(None, 0.0, 0.0)

    monkeypatch.setattr(m, "make_workload", make)
    monkeypatch.setattr(m, "decisions", decide)
    m.calibrate(2)
    calibration = seen.copy()
    assert calibration == [(case, seed) for case in m.NULLS for seed in (10_000, 10_002)]
    assert all(onset is None for _, onset in visited)
    seen.clear()
    for case in (*m.NULLS, *m.FAULTS):
        assert len(list(m.paths(case, 3, holdout=True))) == 3
    assert set(calibration).isdisjoint(seen)
    assert {seed for _, seed in seen} == {10_001, 10_003, 10_005}
    assert [onset for _, onset in visited[-3:]] == [m.ONSET] * 3


def test_both_policies_get_the_same_heldout_path(monkeypatch):
    path = object()
    called = []

    def record(trace, rule, cutoff):
        called.append((trace, rule, cutoff))
        return rule, 3, 10

    monkeypatch.setattr(m, "paths", lambda *args, **kwargs: iter((path,)))
    monkeypatch.setattr(m, "result", record)
    points = {"errors": m.OperatingPoint(0.1, (0, 0)), "joint": m.OperatingPoint(0.2, (0, 0))}
    assert list(m.paired_results("combined", 1, points)) == [(("errors", 3, 10), ("joint", 3, 10))]
    assert called == [(path, "errors", 0.1), (path, "joint", 0.2)]


def test_paired_interval_has_nonzero_uncertainty_at_zero_disagreement():
    counts = m.PairedCounts(2_900, 0, 0, 100)
    low, high = counts.interval(0.975)
    assert low < counts.difference == 0 < high
    assert low == -high
    assert high < m.MARGIN


def test_paired_intervals_reverse_direction_when_swapping_policies():
    counts = m.PairedCounts.count(
        [(False, False)] * 170 + [(False, True)] * 15 + [(True, False)] * 5 + [(True, True)] * 10
    )
    low, high = counts.interval(0.975)
    reverse = m.PairedCounts(counts.neither, counts.joint_only, counts.errors_only, counts.both)
    assert counts.difference == 0.05
    assert low < counts.difference < high
    assert reverse.interval(0.975) == pytest.approx((-high, -low))
    assert high - low >= counts.interval(0.95)[1] - counts.interval(0.95)[0]


def test_both_nulls_must_be_strictly_inside_margin():
    inside = (-0.005, 0.010)
    assert m.risks_match(dict.fromkeys(m.NULLS, inside))
    assert not m.risks_match({m.NULLS[0]: inside})
    for interval in ((-m.MARGIN, 0.0), (0.0, m.MARGIN), (0.0, 0.03), (0.01, -0.01)):
        assert not m.risks_match({m.NULLS[0]: inside, m.NULLS[1]: interval})


@pytest.mark.parametrize(("n", "verdict"), [(1_000, "matched within"), (20, "matched risk not established")])
def test_report_keeps_fault_results_when_matching_is_inconclusive(monkeypatch, capsys, n, verdict):
    monkeypatch.setattr(m, "calibrate", lambda count: dict.fromkeys(m.RULES, m.OperatingPoint(0.1, (0, 0))))

    def pairs(scenario, total, points):
        for _ in range(total):
            yield ((None, None, 8), ("latency", 3, 4) if scenario in m.FAULTS else (None, None, 8))

    monkeypatch.setattr(m, "paired_results", pairs)
    m.run(20, n)
    output = capsys.readouterr().out
    assert f"Verdict: {verdict}" in output
    assert "simultaneous 95% interval" in output and "Paired detection difference" in output
    assert all(scenario in output for scenario in (*m.NULLS, *m.FAULTS))
    assert "missed" in output and "mean delay" in output and "canary sends" in output
