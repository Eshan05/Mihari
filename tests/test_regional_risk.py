from fractions import Fraction

import pytest

from mihari import regional_guardrail as regional
from mihari import regional_risk as risk
from mihari.matched_risk import OperatingPoint


def window(a, b, *, missing=False):
    control = regional.Arm(20, 20, 0)
    return (
        regional.Region(regional.Arm(20, 0, 0) if missing else control, regional.Arm(20, 20, a)),
        regional.Region(control, regional.Arm(20, 20, b)),
    )


def test_branch_hopping_cannot_cross_and_joint_peak_uses_same_branch():
    path = tuple(risk.score_path((window(6, 0), window(0, 6))))
    assert path[1].gaps == (Fraction(9, 40), 0, Fraction(3, 10))
    assert path[1].scores == (Fraction(3, 40), 0, 0)
    assert risk.peaks(path) == (Fraction(3, 40), Fraction(3, 40))
    assert not risk.crossings(path[-1], regional.GAP_CUTOFF, joint=True)


def test_strict_comparison_and_simultaneous_branches():
    _, equal = risk.score_path((window(3, 3), window(3, 3)))
    _, above = risk.score_path((window(4, 4), window(5, 5)))
    assert not risk.crossings(equal, Fraction(3, 20), joint=True)
    assert risk.crossings(above, Fraction(3, 20), joint=True) == ("overall", "A", "B")
    assert risk.crossings(above, Fraction(3, 20), joint=False) == ("overall",)


def test_missing_stratum_holds_every_branch_and_resets():
    all_high = window(8, 8)
    path = tuple(risk.score_path((all_high, window(8, 8, missing=True), all_high, all_high)))
    assert path[1] == risk.Look(None, None)
    assert path[0].scores is path[2].scores is None
    assert risk.crossings(path[3], Fraction(3, 20), joint=True) == ("overall", "A", "B")


def test_global_and_regional_exposure_counts_match_existing_policy_at_unchanged_cutoff():
    for case in (*regional.NULLS, *regional.FAULTS):
        windows = regional.make_windows(case, 1_040_000)
        path = tuple(risk.score_path(windows))
        observed = risk.evaluate(windows, path, case, regional.GAP_CUTOFF, joint=False)
        assert observed.rollout == regional.outcome(windows, case, adjusted=True)


def test_joint_can_fire_on_local_branch_and_counts_sends_through_decision():
    windows = (window(0, 0),) * 3 + (window(8, 0),) * 2 + (window(0, 0),) * 7
    path = tuple(risk.score_path(windows))
    local = risk.evaluate(windows, path, "regional-regression", Fraction(3, 20), joint=True)
    overall = risk.evaluate(windows, path, "regional-regression", Fraction(3, 20), joint=False)
    assert local.branches == ("A",) and local.rollout.trigger == 4
    assert local.rollout.exposed == 60
    assert not overall.branches and overall.rollout.exposed == 80


def test_predeclared_ranges_are_disjoint():
    ranges = (regional.SEEDS, risk.CALIBRATION, risk.NULL_HOLDOUT, risk.FAULT_HOLDOUT)
    assert ranges[1:] == (range(1_010_000, 1_012_000), range(1_020_000, 1_028_000), range(1_040_000, 1_043_000))
    assert len(set().union(*ranges)) == sum(map(len, ranges))


def test_calibration_uses_only_complete_null_peaks_and_strict_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(risk, "CALIBRATION", range(100))

    def make(case, seed):
        calls.append((case, seed))
        if seed < 90:
            return 0, 0
        if seed < 95:
            return Fraction(1, 10), Fraction(3, 10)
        return Fraction(1, 5), Fraction(2, 5)

    monkeypatch.setattr(regional, "make_windows", make)
    monkeypatch.setattr(risk, "score_path", lambda x: x)
    monkeypatch.setattr(risk, "peaks", lambda x: x)
    assert risk.calibrate() == (OperatingPoint(Fraction(1, 10), (5, 5, 5)),
                                OperatingPoint(Fraction(3, 10), (5, 5, 5)))
    assert calls == [(case, seed) for case in regional.NULLS for seed in range(100)]


def test_pair_shares_the_same_workload_and_scores_between_rules(monkeypatch):
    windows = (window(4, 3), window(3, 4))
    calls = []
    points = (OperatingPoint(0, ()), OperatingPoint(1, ()))

    def evaluate(trace, path, case, cutoff, *, joint):
        calls.append((trace, path))
        return cutoff, joint

    monkeypatch.setattr(regional, "make_windows", lambda *_: windows)
    monkeypatch.setattr(risk, "evaluate", evaluate)
    assert risk.pair("regional-regression", 1_040_000, points) == ((0, False), (1, True))
    assert calls[0][0] is calls[1][0] is windows
    assert calls[0][1] is calls[1][1]


def test_simultaneous_calibration_gate_requires_every_null_inside_strict_margin():
    intervals = dict.fromkeys(regional.NULLS, (-0.01, 0.01))
    assert risk.risks_match(intervals)
    last = regional.NULLS[-1]
    intervals[last] = (-risk.MARGIN, 0.01)
    assert not risk.risks_match(intervals)
    del intervals[last]
    assert not risk.risks_match(intervals)


@pytest.mark.parametrize("matched", [True, False])
def test_run_reports_both_faults_even_when_matching_fails(monkeypatch, capsys, matched):
    seen = []
    monkeypatch.setattr(risk, "NULL_HOLDOUT", range(2))
    monkeypatch.setattr(risk, "FAULT_HOLDOUT", range(2, 3))
    monkeypatch.setattr(risk, "pair", lambda case, seed, points: seen.append((case, seed)))
    monkeypatch.setattr(risk, "report", lambda case, pairs: (-0.001, 0.001) if matched else (-0.02, 0.01))
    risk.run(())
    assert ("risks matched within" in capsys.readouterr().out) is matched
    assert seen == [(case, seed) for case in regional.NULLS for seed in range(2)] + [
        (case, 2) for case in regional.FAULTS
    ]
