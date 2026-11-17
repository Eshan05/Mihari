from fractions import Fraction

import numpy as np
import pytest

from mihari import regional_guardrail as regional


def arm(n, slow):
    return regional.Arm(n, n, slow)


def window(control_slow, canary_slow, *, missing=False):
    control = regional.Arm(20, 0, 0) if missing else arm(20, control_slow)
    return regional.Region(control, arm(20, canary_slow)), regional.Region(arm(20, control_slow), arm(20, canary_slow))


def test_same_region_rates_remove_a_pooled_mix_gap():
    mixed = (regional.Region(arm(180, 90), arm(70, 35)),
             regional.Region(arm(720, 72), arm(30, 3)))
    assert regional.gap(mixed, adjusted=False) == Fraction(1, 5)
    assert regional.gap(mixed, adjusted=True) == 0


def test_regional_harm_is_weighted_not_treated_as_worst_region():
    mixed = (regional.Region(arm(180, 18), arm(70, 28)),
             regional.Region(arm(720, 72), arm(30, 3)))
    assert regional.gap(mixed, adjusted=False) == Fraction(21, 100)
    assert regional.gap(mixed, adjusted=True) == Fraction(3, 40)


def test_observation_is_inclusive_at_deadline_and_excludes_late_slow_requests():
    result = regional.observe(np.array([True, False, True, True]), np.array([0, 1, 2, 3]))
    assert result == regional.Arm(4, 3, 2)


@pytest.mark.parametrize("seen", [0, 3])
def test_missing_or_small_region_does_not_get_dropped_or_assumed_safe(seen):
    incomplete = (regional.Region(regional.Arm(20, seen, 0), arm(20, 5)),
                  regional.Region(arm(20, 1), arm(20, 5)))
    assert regional.gap(incomplete, adjusted=False) is not None
    assert regional.gap(incomplete, adjusted=True) is None


def test_strict_crossings_and_missing_region_break_the_run():
    equal, high = window(1, 4), window(1, 5)
    windows = (equal, equal, high, window(1, 5, missing=True), high, high, high)
    looks = tuple(regional.replay(windows, adjusted=True))
    assert [look.gap for look in looks[:4]] == [Fraction(3, 20), Fraction(3, 20), Fraction(1, 5), None]
    assert [look.rollback for look in looks] == [False] * 5 + [True]
    assert looks[-1].at == looks[-1].block + regional.GRACE
    pooled = tuple(regional.replay(windows, adjusted=False))
    assert pooled[-1].rollback and pooled[-1].block == 3


def test_paired_routing_keeps_offered_region_draws_and_only_withholds_a_control():
    balanced = regional.make_windows("balanced-outage", 990_000)
    skewed = regional.make_windows("skewed-outage", 990_000)
    missing = regional.make_windows("missing-a-control", 990_000)
    assert len(skewed) == len(balanced) == regional.LOOKS == 12
    for b, (base, skew, delayed) in enumerate(zip(balanced, skewed, missing, strict=True)):
        assert tuple(r.canary.sent for r in base) == (25, 75)
        assert tuple(r.canary.sent for r in skew) == (70, 30)
        assert tuple(r.control.sent for r in skew) == (180, 720)
        for x, y in zip(base, skew, strict=True):
            assert regional.total((x.control, x.canary)) == regional.total((y.control, y.canary))
        if regional.ONSET <= b < regional.RECOVERY:
            assert delayed[0].control == regional.Arm(180, 0, 0)
            assert delayed[0].canary == skew[0].canary
            assert delayed[1] == skew[1]
        else:
            assert delayed == skew


def test_both_policies_share_the_same_window_object(monkeypatch):
    windows = (window(0, 1),)
    calls = []

    def outcome(trace, case, *, adjusted=False):
        calls.append((trace, case, adjusted))
        return adjusted

    monkeypatch.setattr(regional, "make_windows", lambda case, seed: windows)
    monkeypatch.setattr(regional, "outcome", outcome)
    assert regional.pair("regional-regression", 990_000) == (False, True)
    assert [call[1:] for call in calls] == [("regional-regression", False), ("regional-regression", True)]
    assert all(call[0] is windows for call in calls)


def test_exposure_is_region_specific_and_counts_sends_through_decision():
    quiet, high = window(0, 1), window(1, 6)
    windows = (quiet,) * 3 + (high, high) + (quiet,) * 7
    local = regional.outcome(windows, "regional-regression", adjusted=True)
    global_ = regional.outcome(windows, "global-regression", adjusted=True)
    assert local.trigger == global_.trigger == 4
    assert local.detected and local.delay == 2
    # The tick 6 decision includes sends at ticks 4, 5, and 6.
    assert local.exposed == 60
    assert global_.exposed == 120
    assert regional.outcome(windows, "skewed-outage").exposed == 0


def test_report_distinguishes_early_and_unrelated_stops_from_late_detection(capsys):
    early = regional.Outcome(3, 4, 0, 20)
    late = regional.Outcome(6, 7, 0, 40)
    unrelated = regional.Outcome(8, 9, 1, 40)
    no_stop = regional.Outcome(None, 12, 4, 40)
    assert early.delay == unrelated.delay == no_stop.delay == regional.HORIZON
    assert late.detected and late.delay == 4
    regional.report("regional-regression", [(early, late), (unrelated, no_stop)])
    output = capsys.readouterr().out
    assert "pre-fault stops 1, post-fault evidence stops 1" in output
    assert "detections at/after recovery 1" in output
    assert "ineligible holds 4/19 looks" in output
    assert "Paired detected: pooled only 0, adjusted only 1, both 0, neither 1" in output


def test_scripted_missing_region_timeline_shows_a_hold(capsys):
    regional.show()
    output = capsys.readouterr().out
    assert "skewed-outage" in output and "missing-a-control" in output
    assert "A ctl 0/0/180" in output
    assert "adjusted HOLD (ineligible)" in output
    assert "pooled +16.33 pp ROLLBACK" in output
