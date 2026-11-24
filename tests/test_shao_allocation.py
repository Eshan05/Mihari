from dataclasses import replace

import numpy as np
import pytest
from scipy.stats import t

from mihari import shao_allocation as shao


def test_bounded_discrete_objective_matches_paper_and_canary_tie_break():
    variance = (900, 1225)
    info = tuple(m.information for m in shao.MODELS)

    def paper(count):
        terms = [v / n for n, v in zip((60 - count, count), variance, strict=True)]
        return sum(r / (1 + a * r) for r, a in zip(terms, info, strict=True))

    expected = min(range(6, 31), key=paper)
    assert expected == 15
    assert shao.choose_count("state-space", 5, variance) == (expected, False)
    assert shao.objective(expected, variance, info) == pytest.approx(paper(expected))
    assert shao.choose_count("neyman", 5, variance) == (30, False)
    assert shao.choose_count("neyman", 5, (10000, 1)) == (6, False)
    # n=6 and n=7 both give exactly 60 objective units with these variances.
    assert shao.choose_count("neyman", 5, (2862, 42)) == (6, False)


@pytest.mark.parametrize("prior", [None, (0, 100), (float("inf"), 100), (100, float("nan"))])
def test_burn_in_and_unusable_adaptive_variance_use_fixed_count(prior):
    for policy in shao.POLICIES:
        assert shao.choose_count(policy, 4, (900, 1225)) == (15, False)
        assert shao.choose_count(policy, 5, prior) == (15, policy != "fixed")


def test_shock_changes_canary_level_only_without_redrawing_latents_or_requests():
    assert shao.SEEDS == range(1_120_000, 1_121_500)
    matched = shao.make_tape(shao.SEEDS[0], "matched")
    shift = shao.make_tape(shao.SEEDS[0], "level-shift")
    assert matched.potential.shape == (32, 2, 60)
    np.testing.assert_array_equal(matched.ordering, shift.ordering)
    np.testing.assert_array_equal(np.sort(matched.ordering[0]), np.arange(60))
    np.testing.assert_array_equal(matched.potential[:, 0], shift.potential[:, 0])
    np.testing.assert_array_equal(matched.potential[:16], shift.potential[:16])
    np.testing.assert_allclose(shift.latent[16:, 1] - matched.latent[16:, 1], 25)
    np.testing.assert_allclose(shift.potential[16:, 1] - matched.potential[16:, 1], 25)


def test_past_variance_is_pooled_within_arms_and_periods_and_never_uses_current():
    n, periods = shao.REQUESTS, shao.PERIODS
    ordering = np.tile(np.arange(n), (periods, 1))
    means = np.arange(periods)[:, None, None] * 500
    potential = np.broadcast_to(means + np.arange(n), (periods, 2, n)).copy()
    tape = shao.Tape(np.zeros((periods, 2)), potential, ordering)
    baseline = shao.observe(tape, "neyman")
    lag = (np.var(np.arange(15, 60), ddof=1), np.var(np.arange(15), ddof=1))
    assert all(p.canary == 15 for p in baseline.periods[:4])
    assert baseline.periods[4].prior == pytest.approx(lag)
    altered = potential.copy()
    altered[4, 1] *= 100
    changed = shao.observe(replace(tape, potential=altered), "neyman")
    assert changed.periods[:4] == baseline.periods[:4]
    assert changed.periods[4].prior == baseline.periods[4].prior
    assert changed.periods[4].canary == baseline.periods[4].canary == 15
    assert changed.periods[5].canary == 30


def test_replay_never_reads_unassigned_potential_outcomes():
    tape = shao.make_tape(shao.SEEDS[0], "matched")
    original = shao.observe(tape, "state-space")
    masked = tape.potential.copy()
    for i, period in enumerate(original.periods):
        treatment = tape.ordering[i, :period.canary]
        control = tape.ordering[i, period.canary:]
        masked[i, 0, treatment] = np.nan
        masked[i, 1, control] = np.nan
    assert shao.observe(replace(tape, potential=masked), "state-space") == original


def test_smoother_matches_conditioned_joint_gaussian_directly():
    model = shao.ArmModel(0.6, 0.8, 3, 1.5)
    observed = np.array([1.2, 1.3, -0.3])
    r = np.array([0.7, 2.2, 0.1])
    index = np.arange(1, 4)
    mu = model.prior_mean * model.beta**index
    var = [model.beta**(2 * i) * model.prior_var
           + model.innovation_var * sum(model.beta**(2 * k) for k in range(i)) for i in index]
    cov = np.array([[var[min(i, j)] * model.beta**abs(i - j) for j in range(3)] for i in range(3)])
    gain = np.linalg.solve(cov + np.diag(r), cov).T
    expected = np.column_stack((mu + gain @ (observed - mu), np.diag(cov - gain @ cov)))
    np.testing.assert_allclose(shao.smooth(observed, r, model), expected)


def test_estimators_fail_closed_on_unusable_variance_and_exposure_uses_truth_only_afterward():
    tape = shao.make_tape(shao.SEEDS[0], "level-shift")
    observed = shao.observe(tape, "fixed")
    with pytest.raises(ValueError, match="arm variance"):
        shao.estimates(replace(observed, variance=(0, 1)))
    result = shao.evaluate(tape, observed)
    gaps = tape.latent[:, 1] - tape.latent[:, 0]
    assert result.canary_sends == 32 * 15
    assert result.positive_gap_exposure == pytest.approx(15 * sum(max(0, g) for g in gaps))
    assert shao.estimates(observed)[0] == pytest.approx(np.mean([p.gap for p in observed.periods]))


def test_mse_interval_pairs_seeds_and_adjusts_for_both_locked_scenarios():
    def row(error):
        return shao.Result(0, error, 0, 0, 0, 0, 0)

    classical = [row(1), row(2), row(3)]
    adjusted = [row(2), row(1), row(3)]
    center, low, high = shao.mse_interval(classical, adjusted)
    assert center == 0
    assert high == pytest.approx(t.ppf(0.9875, 2) * np.std([3, -3, 0], ddof=1) / np.sqrt(3))
    assert low == -high
