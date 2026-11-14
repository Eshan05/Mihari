from dataclasses import replace

import numpy as np
import pytest

from mihari import adaptive_steps as a
from mihari import fixed_steps as f


STAGE_TICKS = f.WINDOWS + f.GRACE


@pytest.mark.parametrize(("ratio", "share"), [(0.50, 35), (0.70, 35), (0.80, 25), (0.90, 15), (0.99, 15)])
def test_middle_share_takes_less_headroom_of_both_metrics(ratio, share):
    assert a.middle_for((ratio * f.ERROR_MARGIN, 0)) == share
    assert a.middle_for((0, ratio * f.SLOW_MARGIN)) == share


@pytest.mark.parametrize(("example", "middle"),
                         [("strong-evidence", 35), ("barely-passing", 15), ("blocked", None)])
def test_scripted_paths_keep_first_stage_and_decision_schedule(example, middle):
    baseline, adaptive = a.scripted_pair(example)
    assert adaptive[:STAGE_TICKS] == baseline[:STAGE_TICKS]
    if middle is None:
        assert adaptive == baseline
        assert adaptive[-1].terminal and adaptive[-1].action == "HOLD"
    else:
        assert [adaptive[i].share for i in (0, STAGE_TICKS, STAGE_TICKS * 2)] == [10, middle, 50]
        assert [baseline[i].share for i in (0, STAGE_TICKS, STAGE_TICKS * 2)] == [10, 25, 50]
        assert all(events[-1].action == "PROMOTE" for events in (baseline, adaptive))
        for events in (baseline, adaptive):
            assert [e.tick for e in events if e.action in ("ADVANCE", "PROMOTE")] == [9, 19, 29]
            assert all(events[i].sent_canary == 0 for i in (8, 9, 18, 19, 28, 29))


@pytest.mark.parametrize("scenario", ["early-fault", "shared-degradation", "missing-telemetry"])
def test_failed_first_gate_cannot_choose_middle(scenario):
    def unexpected(evidence):
        pytest.fail("failed first gate called the allocation rule")

    events = tuple(f.timeline(scenario, 0, scripted=True, choose_middle=unexpected))
    assert events[-1].terminal and all(e.share == 10 for e in events)


def preset_draws():
    errors = np.ones(f.REQUESTS)
    errors[[0, 199, 999]] = 0
    return a.Draws(0, np.arange(f.REQUESTS), errors, np.full(f.REQUESTS, 0.10))


def test_pairing_counts_same_ranked_requests_and_applies_share_dependent_hazard():
    draws = preset_draws()
    at_10 = a.sample("healthy", 10, 0, draws)
    at_35 = a.sample("healthy", 35, 0, draws)
    assert (at_10.canary.sent, at_35.canary.sent) == (100, 350)
    assert (at_10.canary.errors, at_35.canary.errors) == (1, 2)
    assert (at_10.control.errors, at_35.control.errors) == (2, 1)
    assert at_10.canary.slow == at_35.canary.slow == 0
    lower = a.sample("larger-step-fault", 15, 0, draws)
    higher = a.sample("larger-step-fault", 25, 0, draws)
    assert lower.canary.slow == 0 and higher.canary.slow == 250
    assert lower.control.slow == higher.control.slow == 0
    late = a.sample("missing-telemetry", 10, f.WINDOWS - 1, draws)
    assert (late.canary.sent, late.canary.seen, late.canary.errors) == (100, 99, 0)


def test_rollouts_share_draws_by_absolute_tick_without_advancing_random_state(monkeypatch):
    seen = []
    draw = a.draw_window

    def measured(seed, tick):
        seen.append((seed, tick))
        return draw(seed, tick)

    monkeypatch.setattr(a, "draw_window", measured)
    baseline, adaptive = a.paired_timeline("healthy", 40_000)
    assert baseline[:STAGE_TICKS] == adaptive[:STAGE_TICKS]
    assert len(seen) == len(set(seen))
    assert all(seed == 40_000 and tick % STAGE_TICKS < f.WINDOWS for seed, tick in seen)
    assert draw(40_000, 0).routing.tolist() == draw(40_000, 0).routing.tolist()
    assert draw(40_000, 0).routing.tolist() != draw(40_000, 1).routing.tolist()


def test_cautious_middle_share_can_defer_hazard_to_final_stage(capsys):
    rng = a.default_rng(0)

    def source(share, block, _tick):
        evidence = f.sample_window("larger-step-fault", share, block, rng, scripted=True)
        if share == 10:
            return replace(evidence, canary=replace(evidence.canary, errors=2 + block % 2))
        return evidence

    baseline, adaptive = a.paired_timeline("larger-step-fault", 0, source=source)
    assert baseline[-1].action == adaptive[-1].action == "ROLLBACK"
    assert baseline[-1].share == 25 and adaptive[-1].share == 50
    assert adaptive[STAGE_TICKS].share == 15 and adaptive[2 * STAGE_TICKS - 1].faulty_canary == 0
    a.write_policy("adaptive", [adaptive], "larger-step-fault")
    assert "first activated at 25%: 0, 35%: 0, 50%: 1" in capsys.readouterr().out


def test_report_separates_reaching_hazard_from_rollback(capsys):
    a.run(2)
    output = capsys.readouterr().out
    assert all(case in output for case in f.SCENARIOS)
    assert "reached fault share" in output and "rolled back after activation" in output
    assert "stopped after 15% without activation" in output and "evaluated final checkpoint" in output
    assert "false rollback" in output and "unsafe promotions" in output
    assert "paired mean difference" in output and "not equal-risk" in output
