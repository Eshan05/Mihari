from dataclasses import replace

import pytest

from mihari import stress
from mihari.rollout import GRACE, ONSET, RULES, make_trace, replay
from mihari.study import outcome


@pytest.mark.parametrize("scenario", (*stress.NULLS, *stress.FAULTS))
def test_availability_profiles_preserve_traffic_and_failures(scenario, monkeypatch):
    generated = []
    make = stress.make_trace

    def generate(*args, **kwargs):
        trace = make(*args, **kwargs)
        generated.append(trace)
        return trace

    monkeypatch.setattr(stress, "make_trace", generate)
    prompt, delayed = stress.simulate(scenario, 42).values()
    assert len(generated) == 1
    trace = generated[0]
    assert all(sum(row.canary for row in window) * 10 == len(window) for window in trace)
    for profile in stress.PROFILES:
        timed = stress.retime(trace, profile, 42)
        assert all(replace(after, available_at=before.available_at) == before
                   for window, new in zip(trace, timed, strict=True)
                   for before, after in zip(window, new, strict=True))
    assert prompt[1:] == (0, 0)
    assert delayed[1] > 0 and 0 < delayed[2] <= GRACE
    assert [d.look.block for d in prompt[0]] == [d.look.block for d in delayed[0]]


def test_later_window_waits_for_earlier_window_and_late_outcome_is_never_used():
    windows = make_trace("delayed-regression", 0, scripted=True)[ONSET:ONSET + 3]
    trace = tuple(tuple(replace(row, block=i, available_at=i + (
        GRACE + 1 if i == 0 and row.canary and row.pair == 0 else 0))
        for row in window) for i, window in enumerate(windows))
    first, second, third = replay(trace, 0, complete_early=True, fault_end=1)
    assert [decision.look.block for decision in (first, second, third)] == [0, 1, 2]
    assert [decision.look.at for decision in (first, second, third)] == [2, 2, 2]
    assert first.look.canary == first.look.canary_only == 3
    assert first.threshold_score is first.sequential_score is None
    assert second.threshold_score is None
    assert third.rollback("threshold", 0.2)
    assert len(tuple(replay(trace, complete_early=True))) == 3
    assert third.look.exposed == 4


def test_early_close_waits_until_delayed_outcomes_are_available():
    base = make_trace("healthy", 0, scripted=True)[:1]
    trace = (tuple(replace(row, available_at=1 if row.canary else 0) for row in base[0]),)
    early, = replay(trace, complete_early=True)
    fixed, = replay(trace)
    assert (early.look.at, early.look.canary) == (1, 4)
    assert fixed.look.at == GRACE


def test_calibration_cutoff_applies_to_both_profiles_on_separate_trials(monkeypatch):
    keys = []
    simulate = stress.simulate

    def record(scenario, seed):
        keys.append((scenario, seed))
        return simulate(scenario, seed)

    monkeypatch.setattr(stress, "simulate", record)
    limits = stress.cutoffs(20)
    assert set(limits) == set(RULES)
    assert {case for case, _ in keys} == set(stress.NULLS)
    assert len(keys) == len(set(keys)) == 40
    assert {seed % 2 for _, seed in keys} == {0}
    keys.clear()
    for case in (*stress.NULLS, *stress.FAULTS):
        pair, = stress.rollouts(case, 1, holdout=True)
        assert tuple(pair) == stress.PROFILES
    assert {seed % 2 for _, seed in keys} == {1}


def test_short_fault_recovers_and_only_fault_window_exposure_counts():
    trace = make_trace("delayed-regression", 0, scripted=True, duration=1)
    assert [i for i, window in enumerate(trace) if any(row.failed for row in window)] == [ONSET]
    decisions = tuple(replay(stress.retime(trace, "prompt", 0), ONSET, complete_early=True,
                             fault_end=ONSET + 1))
    stop, delay, exposed = outcome(decisions, "sequential", 100)
    assert stop is delay is None
    assert exposed == 4


def test_stress_report_includes_recovery_and_pairs(capsys):
    stress.run(20)
    report = capsys.readouterr().out
    assert "healthy" in report and "shared-outage" in report and "false rollback" in report
    assert "weak-sustained" in report and "one-window" in report
    assert "at or after recovery" in report and "paired detections" in report
    assert "excluded at close" in report and "affected-window canary sends" in report
