import pytest

from mihari import adaptive_steps as adaptive
from mihari import fixed_steps as fixed
from mihari import staged_risk as staged


def test_fault_onset_uses_the_clock_or_the_share_not_both():
    assert staged.MIDDLE_TICK == 10
    assert staged.fault("common-time", 35, 9) == (0, 0)
    assert staged.fault("common-time", 15, 10) == (0, 0.22)
    assert staged.fault("common-time", 50, 20) == (0, 0.22)
    assert staged.fault("share-triggered", 15, 20) == (0, 0)
    assert staged.fault("share-triggered", 25, 10) == (0, 0.22)
    assert staged.fault("shared-degradation", 50, 20) == (0, 0)


def test_custom_stage_harm_requires_a_matching_sample_source():
    with pytest.raises(ValueError, match="matching window source"):
        tuple(fixed.timeline("healthy", 1, stage_harm=lambda *_: (0, 0.22)))


@pytest.mark.parametrize(("new", "old"), [
    ("healthy", "healthy"),
    ("shared-degradation", "shared-degradation"),
    ("share-triggered", "larger-step-fault"),
])
def test_comparators_reproduce_existing_paired_paths(new, old):
    for seed in (40_000, 40_001, 40_002):
        assert staged.paired_timeline(new, seed) == adaptive.paired_timeline(old, seed)


@pytest.mark.parametrize(("case", "adaptive_onset"), [("common-time", (10, 15)), ("share-triggered", (20, 50))])
def test_scripted_paths_share_first_stage_and_charge_sends_before_evidence(case, adaptive_onset):
    original, changed = staged.paired_timeline(case, 0, scripted=True)
    assert original[:10] == changed[:10]
    assert (original[10].share, changed[10].share) == (25, 15)
    baseline = staged.outcome(original)
    result = staged.outcome(changed)
    assert baseline.activation == (10, 25)
    assert result.activation == adaptive_onset
    tick, share = adaptive_onset
    first = changed[tick]
    assert first.evidence == fixed.Evidence()
    assert first.faulty_canary == first.sent_canary == fixed.REQUESTS * share // 100
    assert changed[tick + 1].evidence == fixed.Evidence()
    assert changed[tick + 2].evidence.canary.slow > 0
    assert result.delay == fixed.GRACE + 1
    assert result.exposed == first.sent_canary * (result.delay + 1)


def test_fault_report_does_not_confuse_holds_nonactivation_and_detection(capsys):
    pre_fault_rollback = staged.Outcome("ROLLBACK", None, None, 0)
    safe_hold = staged.Outcome("HOLD", None, None, 0)
    exposed_hold = staged.Outcome("HOLD", (10, 25), None, 250)
    detected = staged.Outcome("ROLLBACK", (10, 15), 3, 600)
    staged.report_fault([(exposed_hold, detected), (pre_fault_rollback, safe_hold)])
    report = capsys.readouterr().out
    assert "never activated 1/2 (pre-fault rollbacks 1); detected 0; activated misses 1" in report
    assert "never activated 1/2 (pre-fault rollbacks 0); detected 1; activated misses 0" in report
    assert "+175.0 / all 2 pairs; +350.0 / 1 jointly activated pairs" in report


def test_fault_report_handles_no_joint_activations(capsys):
    safe_hold = staged.Outcome("HOLD", None, None, 0)
    exposed_hold = staged.Outcome("HOLD", (10, 25), None, 250)
    staged.report_fault([(exposed_hold, safe_hold)])
    report = capsys.readouterr().out
    assert "fixed only 1, adaptive only 0, both 0, neither 0" in report
    assert "-250.0 / all 1 pairs; n/a / 0 jointly activated pairs" in report


def test_null_holds_and_promotion_are_not_false_rollbacks(capsys):
    hold = staged.Outcome("HOLD", None, None, 0)
    promote = staged.Outcome("PROMOTE", None, None, 0)
    low, high = staged.report_null([(hold, promote)] * 2_000)
    report = capsys.readouterr().out
    assert report.count("false rollback 0/2000") == 2
    assert "promote 0; terminal hold 2000" in report
    assert "promote 2000; terminal hold 0" in report
    assert -staged.MARGIN < low < 0 < high < staged.MARGIN


def test_inconclusive_null_risk_still_reports_both_faults(monkeypatch, capsys):
    intervals = iter(((-0.001, 0.001), (-staged.MARGIN, 0.001)))
    reported = []
    monkeypatch.setattr(staged, "SEEDS", range(1))
    monkeypatch.setattr(staged, "paired_timeline", lambda case, seed: (case, case))
    monkeypatch.setattr(staged, "outcome", lambda result: result)
    monkeypatch.setattr(staged, "report_null", lambda pairs: next(intervals))
    monkeypatch.setattr(staged, "report_fault", lambda pairs: reported.extend(pairs))
    staged.run()
    assert "matched risk not established" in capsys.readouterr().out
    assert reported == [(case, case) for case in staged.FAULTS]
