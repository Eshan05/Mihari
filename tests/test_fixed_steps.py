import pytest
from scipy.stats import binomtest

from mihari import fixed_steps as f


def simulated(case):
    return tuple(f.timeline(case, 0, scripted=True))


def test_healthy_has_exact_shares_fresh_evidence_and_control_only_drain():
    events = simulated("healthy")
    assert len(events) == 30
    assert [event.action for event in events if event.action != "HOLD"] == ["ADVANCE", "ADVANCE", "PROMOTE"]
    assert [event.tick for event in events if event.action != "HOLD"] == [9, 19, 29]
    assert [event.tick for event in events if event.terminal] == [29]
    for i, share in enumerate(f.SHARES):
        stage = events[i * 10:(i + 1) * 10]
        assert all(event.share == share for event in stage)
        assert [(event.sent_control, event.sent_canary) for event in stage[:8]] == [
            (1000 - 10 * share, 10 * share)
        ] * 8
        assert [(event.sent_control, event.sent_canary) for event in stage[8:]] == [(1000, 0)] * 2
        assert stage[0].evidence == stage[1].evidence == f.Evidence()
        assert stage[2].evidence.canary.sent == 10 * share
        assert stage[-1].evidence.canary.sent == stage[-1].evidence.canary.seen == 80 * share
        assert stage[-1].evidence.control.sent == stage[-1].evidence.control.seen == 8000 - 80 * share
    assert events[-1].cumulative_canary == 6800
    assert events[-1].faulty_canary == 0


@pytest.mark.parametrize(("case", "reason", "tick", "share", "exposed"), [
    ("early-fault", "errors", 3, 10, 400),
    ("larger-step-fault", "latency", 13, 25, 1_000),
])
def test_rollback_only_after_observed_fault_and_stops_canary(case, reason, tick, share, exposed):
    events = simulated(case)
    final = events[-1]
    assert final.terminal and final.action == "ROLLBACK"
    assert (final.tick, final.share, final.faulty_canary) == (tick, share, exposed)
    assert final.reason == f"joint guardrail: {reason}"
    assert events[tick - 1].action == "HOLD"
    if case == "larger-step-fault":
        assert events[9].action == "ADVANCE" and events[9].faulty_canary == 0
        assert final.cumulative_canary == 1800
    assert all(event.action != "PROMOTE" for event in events)


@pytest.mark.parametrize(("case", "reason"), [
    ("shared-degradation", "control is degraded"),
    ("missing-telemetry", "incomplete on-time telemetry"),
    ("near-margin", "harm limits not established"),
])
def test_terminal_holds_are_not_promotions_or_rollbacks(case, reason):
    events = simulated(case)
    assert all(event.action == "HOLD" for event in events)
    assert [event.tick for event in events if event.terminal] == [9]
    assert reason in events[-1].reason
    assert events[-1].sent_canary == 0
    if case == "missing-telemetry":
        assert (events[-1].evidence.canary.seen, events[-1].evidence.canary.sent) == (799, 800)


def test_no_rollback_does_not_supply_enough_evidence_to_advance():
    small = f.Evidence(f.Arm(10, 10), f.Arm(10, 10))
    action, reason = f.checkpoint(small, final=False)
    assert action == "HOLD" and "harm limits not established" in reason


@pytest.mark.parametrize(("error", "slow"), [(f.ERROR_MARGIN, 0), (0, f.SLOW_MARGIN)])
def test_equality_to_either_harm_margin_holds(monkeypatch, error, slow):
    bounds = iter((error, slow))
    monkeypatch.setattr(f, "upper_difference", lambda *counts: next(bounds))
    complete = f.Evidence(f.Arm(1000, 1000, 10, 10), f.Arm(100, 100, 1, 1))
    assert f.checkpoint(complete, final=True)[0] == "HOLD"


def test_one_sided_bounds_match_scipy_and_use_stage_family_allowance():
    assert f.ARM_ALPHA * 12 == pytest.approx(0.05)
    upper = binomtest(16, 800, alternative="less").proportion_ci(1 - f.ARM_ALPHA).high
    lower = binomtest(144, 7200, alternative="greater").proportion_ci(1 - f.ARM_ALPHA).low
    assert f.upper_difference(16, 800, 144, 7200) == pytest.approx(upper - lower)
    assert f.upper_difference(10, 10, 0, 10) == 1.0


def test_rollback_at_checkpoint_takes_precedence(monkeypatch):
    sample = f.sample_window

    def last_two(case, share, block, rng, *, scripted):
        return sample("early-fault" if block >= f.WINDOWS - 2 else "healthy",
                      share, block, rng, scripted=scripted)

    def no_checkpoint(*args, **kwargs):
        pytest.fail("a rollback at the checkpoint must not evaluate promotion")

    monkeypatch.setattr(f, "sample_window", last_two)
    monkeypatch.setattr(f, "checkpoint", no_checkpoint)
    final = simulated("healthy")[-1]
    assert final.tick == 9 and final.action == "ROLLBACK"


def test_guardrail_streak_does_not_cross_stage_boundary(monkeypatch):
    sample = f.sample_window

    def boundary_faults(case, share, block, rng, *, scripted):
        affected = (share == 10 and block == f.WINDOWS - 1) or (share == 25 and block == 0)
        return sample("early-fault" if affected else "healthy", share, block, rng, scripted=scripted)

    monkeypatch.setattr(f, "sample_window", boundary_faults)
    monkeypatch.setattr(f, "checkpoint", lambda evidence, *, final: ("PROMOTE" if final else "ADVANCE", "test"))
    events = simulated("healthy")
    assert events[9].action == "ADVANCE" and events[12].action == "HOLD"
    assert all(event.action != "ROLLBACK" for event in events)


def test_unsafe_labels_use_scenario_truth_instead_of_promotion_guard():
    assert f.harm("larger-step-fault", 10) == (0, 0)
    assert f.harm("larger-step-fault", 25)[1] > f.SLOW_MARGIN


def test_forced_unsafe_advances_and_promotion_are_counted(monkeypatch):
    monkeypatch.setattr(f, "checkpoint", lambda evidence, *, final: ("PROMOTE" if final else "ADVANCE", "test"))
    risky = simulated("near-margin")
    assert [event.action for event in risky if event.unsafe] == ["ADVANCE", "ADVANCE", "PROMOTE"]


def test_benchmark_reports_outcomes_time_and_risk(capsys):
    f.run(2)
    output = capsys.readouterr().out
    assert all(case in output for case in f.SCENARIOS)
    assert "promoted" in output and "terminal holds" in output and "mean time to terminal outcome" in output
    assert "false rollback" in output and "unsafe promotions" in output and "faulty-window canary sends" in output
