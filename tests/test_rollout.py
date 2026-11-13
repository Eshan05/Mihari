from pathlib import Path

import pytest

from mihari import experiment, study
from mihari.rollout import GRACE, ONSET, SCENARIOS, Look, Monitor, make_trace, observe, replay


def test_original_report_still_reproduces(tmp_path, monkeypatch):
    archived = Path(__file__).resolve().parents[1] / "results" / "report.md"
    monkeypatch.chdir(tmp_path)
    experiment.main()
    assert (tmp_path / "results" / "report.md").read_text() == archived.read_text()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fixed_exposure_and_scenarios(scenario):
    trace = make_trace(scenario, 0, scripted=True)
    assert all(sum(row.canary for row in block) * 10 == len(block) for block in trace)
    look = list(replay(trace, ONSET))[ONSET].look
    assert look.at == ONSET + GRACE
    expected = {"healthy": (0, 0), "shared-outage": (9, 1), "delayed-regression": (0, 4)}
    assert (look.control_failures, look.canary_failures) == expected[scenario]


def test_unavailable_outcomes_never_enter_counts_or_pairs():
    rows = make_trace("delayed-regression", 0, scripted=True)[ONSET]
    monitor = Monitor()
    for tick in range(ONSET, ONSET + GRACE):
        look = observe(rows, ONSET, tick, 4, 4)
        assert (look.canary, look.canary_failures, look.canary_slow, look.canary_only) == (0, 0, 0, 0)
        decision = monitor.update(look)
        assert decision.threshold_score is decision.sequential_score is None
    look = observe(rows, ONSET, ONSET + GRACE, 4, 4)
    assert (look.canary, look.canary_failures, look.canary_slow, look.canary_only) == (4, 4, 4, 4)
    assert monitor.update(look).sequential_score > 0


def test_gap_requires_consecutive_eligible_windows():
    monitor = Monitor()
    bad = Look(0, 2, 36, 4, 0, 4, 0, 4, 4, 0, 4, 4)
    missing = Look(1, 3, 36, 0, 0, 0, 0, 0, 0, 0, 8, 8)
    assert not monitor.update(bad).rollback("threshold", 0.20)
    assert not monitor.update(missing).rollback("threshold", 0.20)
    assert not monitor.update(bad).rollback("threshold", 0.20)
    assert monitor.update(bad).rollback("threshold", 0.20)


def test_disjoint_calibration_and_holdout_seeds(monkeypatch):
    recorded = []
    original = study.make_trace

    def trace(scenario, seed):
        recorded.append((scenario, seed))
        return original(scenario, seed)

    monkeypatch.setattr(study, "make_trace", trace)
    cutoffs = study.calibrate(20)
    calibration = set(recorded)
    assert len(calibration) == 40
    recorded.clear()
    holdout = list(study.traces("shared-outage", 20, holdout=True))
    assert len(recorded) == len(holdout) == 20
    assert calibration.isdisjoint(recorded)
    assert all(0 <= cutoff for cutoff in cutoffs.values())
    assert all(decision.threshold_score is not None and decision.sequential_score is not None
               for trial in holdout for decision in trial[1:])


def test_missed_early_and_detected_rollouts_preserve_exposure():
    trace = tuple(replay(make_trace("delayed-regression", 0, scripted=True), ONSET))
    stop, delay, exposed = study.outcome(trace, "threshold", 0.20)
    assert (stop.block, delay, exposed) == (ONSET + 1, GRACE + 1, 4 * (GRACE + 2))
    stop, delay, exposed = study.outcome(trace, "threshold", 2.0)
    assert stop is delay is None
    assert exposed == trace[-1].look.exposed == 32
    stop, delay, _ = study.outcome(trace[ONSET - 1:], "threshold", -1.0)
    assert stop.at >= ONSET and stop.block < ONSET and delay is None
