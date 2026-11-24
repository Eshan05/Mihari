import pytest

from mihari import fixed_steps as fixed
from mihari import slo_gate as slo


def window(errors, slow=0, *, seen=100):
    return fixed.Evidence(fixed.Arm(100, seen, errors, slow))


@pytest.mark.parametrize("closed", [
    (window(20),) * 3,
    (window(8, 15),) * 4,
    (window(40),) * 2 + (window(2),) * 2,
    (window(0),) * 2 + (window(10),) * 2,
    (window(20, seen=80),) + (window(20),) * 3,
    (window(20), window(20), window(0, 20), window(0, 20)),
])
def test_needs_complete_windows_and_the_same_metric_above_both_thresholds(closed):
    assert slo.health_hold(closed) is None


def test_weights_by_observed_requests_and_checks_the_latency_budget():
    assert slo.health_hold((window(2, 20),) * 4).startswith("SLO gate: control latency burn")
    small = fixed.Evidence(fixed.Arm(10, 10, 10, 0))
    large = fixed.Evidence(fixed.Arm(1000, 1000, 20, 0))
    assert slo.health_hold((small, large, small, large)) is None


def test_sustained_shared_fault_holds_early_without_rolling_back():
    baseline, guarded = slo.paired_timeline("sustained-shared", 0, scripted=True)
    early = guarded[-1]
    assert baseline[-1].action == early.action == "HOLD"
    assert early.tick == slo.LONG - 1 + fixed.GRACE
    assert early.evidence.control.sent == slo.LONG * 900
    assert early.cumulative_canary == 600 < baseline[-1].cumulative_canary
    assert early.faulty_canary == 0  # Shared slowdown, not a canary regression.
    assert guarded[:-1] == baseline[:early.tick]
    assert baseline[early.tick].evidence == early.evidence


def test_same_tick_canary_rollback_takes_priority_over_the_slo_hold():
    def source(share, block, tick):
        return fixed.Evidence(fixed.Arm(900, 900, 117, 0),
                              fixed.Arm(100, 100, 13 if block < 2 else 40, 0))

    events = tuple(fixed.timeline("healthy", 0, window_source=source, health_gate=slo.health_hold))
    assert events[-1].tick == slo.LONG - 1 + fixed.GRACE
    assert events[-1].action == "ROLLBACK"


def test_one_short_late_window_does_not_get_to_use_future_evidence():
    baseline, guarded = slo.paired_timeline("late-shared", 0, scripted=True)
    adv = next(event for event in guarded if event.action == "ADVANCE")
    assert adv.share == 10 and adv.tick == 9
    assert guarded[-1].share == 25 and guarded[-1].tick == 15
    assert guarded[-1].cumulative_canary < baseline[-1].cumulative_canary


def test_delayed_evidence_can_trigger_a_hold_after_the_service_recovers():
    baseline, guarded = slo.paired_timeline("recovered-shared", 0, scripted=True)
    early = guarded[-1]
    assert early.tick < baseline[-1].tick
    assert slo.shared_rates("recovered-shared", early.tick) == slo.shared_rates("healthy", early.tick)
    assert early.action == "HOLD" and early.reason.startswith("SLO gate:")


@pytest.mark.parametrize(("case", "action"), [("healthy", "PROMOTE"), ("canary-only", "ROLLBACK")])
def test_gate_leaves_healthy_and_canary_only_timelines_unchanged(case, action):
    baseline, guarded = slo.paired_timeline(case, 0, scripted=True)
    assert baseline == guarded
    assert guarded[-1].action == action
