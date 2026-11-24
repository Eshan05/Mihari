from fractions import Fraction
from math import sqrt

import pytest

from mihari import regional_guardrail as regional
from mihari import regional_precision as precision
from mihari import regional_risk as raw
from mihari.matched_risk import OperatingPoint
from mihari.rollout import GRACE, ONSET


def region(control_slow, canary_slow, *, n=20, control_seen=None):
    control_seen = n if control_seen is None else control_seen
    return regional.Region(regional.Arm(n, control_seen, control_slow), regional.Arm(n, n, canary_slow))


def test_same_gap_with_more_samples_is_stronger_and_overall_uses_squared_weights():
    a = region(2, 4)
    b = region(30, 50, n=200)
    gap_a, var_a = precision.gap_variance(a)
    gap_b, var_b = precision.gap_variance(b)
    assert (gap_a, gap_b) == pytest.approx((0.1, 0.1))
    pooled_a, pooled_b = 6.5 / 41, 80.5 / 401
    assert var_a == pytest.approx(pooled_a * (1 - pooled_a) * 2 / 20)
    assert var_b == pytest.approx(pooled_b * (1 - pooled_b) * 2 / 200)
    overall, local_a, local_b = precision.precision_gaps((a, b))
    assert overall == pytest.approx(0.1 / sqrt(0.25**2 * var_a + 0.75**2 * var_b))
    assert local_a == pytest.approx(gap_a / sqrt(var_a))
    assert local_b == pytest.approx(gap_b / sqrt(var_b))
    assert local_b > local_a


@pytest.mark.parametrize("slow", [0, 4])
def test_extreme_counts_have_positive_variance_and_four_seen_is_eligible(slow):
    arm = region(slow, slow, n=4)
    assert precision.gap_variance(arm)[1] > 0
    assert precision.precision_gaps((arm, arm)) == (0, 0, 0)
    assert precision.precision_gaps((region(0, 0, n=4, control_seen=3), arm)) is None


def test_branch_hopping_is_not_persistence_and_missing_evidence_resets():
    flat, hot = region(0, 0), region(0, 12)
    a, b = (hot, flat), (flat, hot)
    missing = (region(0, 12, control_seen=0), flat)
    path = tuple(precision.score_path((a, b, missing, a, a)))
    assert path[1].scores[1:] == (0, 0)
    assert raw.crossings(path[1], 0, joint=True) == ("overall",)
    assert path[2] == precision.Look(None, None)
    assert path[3].scores is None
    assert not raw.crossings(path[4], path[4].scores[1], joint=True)
    assert raw.crossings(path[4], path[4].scores[0], joint=True) == ("A",)


def test_joint_exposure_uses_sent_not_seen_through_decision():
    cold = region(0, 0)
    hot = regional.Region(cold.control, regional.Arm(40, 20, 12))
    windows = ((cold, cold),) * ONSET + ((hot, cold),) * 4 + ((cold, cold),)
    path = tuple(precision.score_path(windows))
    cutoff = path[ONSET + 1].scores[0]
    result = raw.evaluate(windows, path, "regional-regression", cutoff, joint=True)
    assert result.branches == ("A",)
    assert result.rollout.trigger == ONSET + 1
    assert result.rollout.exposed == 40 * (GRACE + 2)
    overall = raw.evaluate(windows, path, "regional-regression", cutoff, joint=False)
    assert overall.rollout.trigger is None


def test_calibration_is_split_and_controls_the_entire_strict_policy(monkeypatch):
    cohorts = precision.CALIBRATION, precision.NULL_HOLDOUT, precision.FAULT_HOLDOUT
    assert cohorts == (range(1_050_000, 1_052_000), range(1_060_000, 1_068_000), range(1_080_000, 1_083_000))
    assert len(set().union(*cohorts, raw.CALIBRATION, raw.NULL_HOLDOUT, raw.FAULT_HOLDOUT)) == 26_000

    calls = []

    def make(case, seed):
        calls.append((case, seed))
        score = 0 if seed < 90 else 1 if seed < 95 else 2
        return (precision.Look((0, 0, 0), (score, 2 * score, 0)),)

    monkeypatch.setattr(precision, "CALIBRATION", range(100))
    monkeypatch.setattr(regional, "make_windows", make)
    monkeypatch.setattr(precision, "score_path", iter)
    assert precision.calibrate() == (OperatingPoint(1, (5, 5, 5)), OperatingPoint(2, (5, 5, 5)))
    assert calls == [(case, seed) for case in regional.NULLS for seed in range(100)]


def test_raw_controls_reuse_exact_cutoffs_and_the_same_held_out_windows(monkeypatch):
    original = regional.make_windows("global-regression", precision.FAULT_HOLDOUT[0])
    calls = []
    seed = precision.FAULT_HOLDOUT[0]
    points = (OperatingPoint(1, ()), OperatingPoint(2, ()))
    assert precision.RAW_CUTOFFS == (Fraction(68_419, 1_062_600), Fraction(465, 3_268))
    with monkeypatch.context() as patch:
        patch.setattr(regional, "make_windows", lambda case, seed: calls.append((case, seed)) or original)
        output = precision.pair("global-regression", seed, points)
    assert calls == [("global-regression", seed)]
    frozen_points = tuple(OperatingPoint(c, ()) for c in precision.RAW_CUTOFFS)
    assert output[2:] == raw.pair("global-regression", seed, frozen_points)
