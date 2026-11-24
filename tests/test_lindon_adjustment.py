from math import isinf, log, sqrt

import numpy as np
import pytest

from mihari import lindon_adjustment as lindon


def test_ols_and_hc1_against_balanced_hand_calculations():
    treatment = np.repeat([0, 1], 4)
    feature = np.tile([0, 0, 1, 1], 2)
    design = np.column_stack((np.ones(8), treatment, feature))
    y = np.array([1, 3, 7, 13, 3, 9, 13, 15], dtype=float)
    # The cell means are 2, 10, 6, 14. Treatment weights are +/-1/4.
    # The adjusted residual sum of squares is 40; the unadjusted one is 168.
    assert lindon.treatment_fit(design, y) == pytest.approx((4, sqrt(8 / 5 * 40 / 16)))
    assert lindon.treatment_fit(design[:, :2], y) == pytest.approx((4, sqrt(8 / 6 * 168 / 16)))


@pytest.mark.parametrize("n, columns", [(200, 2), (400, 3), (4000, 3)])
def test_radius_inverts_scalar_t_evidence(n, columns):
    radius = lindon.critical_radius(n, columns)
    nu = n - columns
    a = lindon.G / (lindon.G + n)
    q = radius**2
    direct = a**0.5 * ((1 + a * q / nu) / (1 + q / nu)) ** (-(nu + 1) / 2)
    assert direct == pytest.approx(1 / lindon.ALPHA)
    assert lindon.log_evidence(n, columns, q) == pytest.approx(log(direct))
    assert lindon.log_evidence(n, columns, q * 0.99) < log(direct)
    assert lindon.log_evidence(n, columns, q * 1.01) > log(direct)


def test_hold_without_treatment_rank_with_zero_variance_or_unbounded_start():
    missing = np.column_stack((np.ones(200), np.zeros(200)))
    t = np.tile([0, 1], 100)
    rank_deficient = np.column_stack((np.ones(200), t, t))
    regular = np.column_stack((np.ones(200), t))
    y = np.arange(200, dtype=float)
    assert lindon.assess(missing, y).action == "HOLD"
    assert lindon.assess(rank_deficient, y).action == "HOLD"
    assert lindon.assess(regular, 2 + 3 * t).action == "HOLD"
    assert lindon.assess(regular, np.full(200, np.nan)).action == "HOLD"
    early = lindon.assess(regular[:8], y[:8])
    assert early.effect is not None and early.action == "HOLD"
    assert isinf(lindon.critical_radius(8, 2))


def test_harm_uses_strict_lower_bound_and_never_stops_for_improvement(monkeypatch):
    design = np.ones((200, 2))
    radius = lindon.critical_radius(*design.shape)
    for estimate, expected in ((radius, "continue"), (radius + 0.01, "ROLLBACK"), (-2 * radius, "continue")):
        monkeypatch.setattr(lindon, "treatment_fit", lambda *_: (estimate, 1))
        assert lindon.assess(design, np.ones(200)).action == expected


def test_simulation_keeps_assignment_and_outcomes_fixed_between_attribute_scenarios():
    seed = lindon.FAULT_SEEDS[0]
    useful = lindon.make_workload(seed, informative=True, effect=lindon.REGRESSION_MS)
    irrelevant = lindon.make_workload(seed, informative=False, effect=lindon.REGRESSION_MS)
    healthy = lindon.make_workload(seed, informative=True, effect=0)
    assert (lindon.LOOKS, lindon.PER_LOOK, lindon.CANARY_SHARE, lindon.REGRESSION_MS) == (20, 200, 0.1, 12)
    assert len(useful.latency) == 4000
    np.testing.assert_array_equal(useful.treatment, irrelevant.treatment)
    np.testing.assert_array_equal(useful.latency, irrelevant.latency)
    np.testing.assert_array_equal(useful.treatment, healthy.treatment)
    np.testing.assert_allclose(useful.latency - healthy.latency, 12 * useful.treatment)
    assert np.any(useful.covariate != irrelevant.covariate)
    assert abs(np.corrcoef(useful.latency, useful.covariate)[0, 1]) > 0.8
    assert abs(np.corrcoef(irrelevant.latency, irrelevant.covariate)[0, 1]) < 0.05
    assert lindon.NULL_SEEDS == range(1_090_000, 1_092_000)
    assert lindon.FAULT_SEEDS == range(1_100_000, 1_102_000)


def test_replay_shares_visible_prefixes_stops_once_and_charges_through_decision(monkeypatch):
    monkeypatch.setattr(lindon, "PER_LOOK", 4)
    monkeypatch.setattr(lindon, "LOOKS", 3)
    treatment = np.tile([False, True], 6)
    covariate = np.tile([1, 1, 0, 0], 3)
    latency = np.arange(12)
    workload = lindon.Workload(treatment, covariate, latency)
    received = []

    def observe(design, y):
        received.append((design, y))
        lower = 1 if design.shape == (8, 3) else -1
        return lindon.Assessment(1, 1, lower)

    monkeypatch.setattr(lindon, "assess", observe)
    unadjusted, adjusted = lindon.run_pair(workload)
    assert unadjusted == lindon.Result(None, 6) and unadjusted.time == 4
    assert adjusted == lindon.Result(2, 4) and adjusted.time == 2
    assert [d.shape for d, _ in received] == [(4, 2), (4, 3), (8, 2), (8, 3), (12, 2)]
    for d, y in received:
        np.testing.assert_array_equal(y, latency[:len(y)])
        np.testing.assert_array_equal(d[:, 1], treatment[:len(y)])
        if d.shape[1] == 3:
            np.testing.assert_array_equal(d[:, 2], covariate[:len(y)])
