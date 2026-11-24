from itertools import combinations
from math import comb

import numpy as np
import pytest
from scipy.stats import fisher_exact

from mihari import clustered_requests as c


def test_burst_generator_preserves_mean_but_increases_variance():
    independent, dependent = (np.concatenate([
        c.sample(seed, bursts=bursts, fault=False)[0] for seed in range(600)
    ]) for bursts in (False, True))
    for errors in (independent, dependent):
        assert errors.mean() == pytest.approx(c.REQUESTS * c.BASE_ERROR, abs=0.12)
    expected_ratio = 1 + (c.ATTEMPTS - 1) * c.BURST_CHANCE
    assert dependent.var() / independent.var() == pytest.approx(expected_ratio, rel=0.15)


def test_same_checkpoint_agrees_with_independent_reference_calculations():
    errors, canary = c.sample(123, bursts=True, fault=True)
    observed = int(errors[canary].sum())
    control = int(errors.sum()) - observed
    n_canary = c.CANARY_TENANTS * c.REQUESTS
    n_control = (c.TENANTS - c.CANARY_TENANTS) * c.REQUESTS
    request, tenant = c.p_values(errors, canary)
    assert request == pytest.approx(fisher_exact([
        [observed, n_canary - observed], [control, n_control - control],
    ], alternative="greater").pvalue)
    expected = sum(sum(errors[i] for i in group) >= observed
                   for group in combinations(range(c.TENANTS), c.CANARY_TENANTS))
    assert tenant == expected / comb(c.TENANTS, c.CANARY_TENANTS)


@pytest.mark.parametrize("value", [0, 2, c.REQUESTS])
def test_tied_tenant_totals_are_not_evidence_against_canary(value):
    errors = np.full(c.TENANTS, value)
    assert c.p_values(errors, np.arange(c.CANARY_TENANTS))[1] == 1.0


def test_one_tenant_retry_burst_does_not_count_as_many_independent_bad_assignments(capsys):
    errors = np.full(c.TENANTS, 2)
    errors[0] += c.ATTEMPTS
    request, tenant = c.p_values(errors, np.arange(c.CANARY_TENANTS))
    assert request < c.ALPHA < tenant
    assert tenant == pytest.approx(c.CANARY_TENANTS / c.TENANTS)
    c.show()
    output = capsys.readouterr().out
    assert "Request-level:" in output and "-> ROLLBACK" in output
    assert "Tenant-level:" in output and "-> NO ROLLBACK" in output


def test_conditional_false_rollback_rate_is_bounded_over_all_valid_assignments():
    errors = np.arange(c.TENANTS)
    sums = errors[c.ASSIGNMENTS].sum(axis=1)
    reject = np.zeros(len(sums), dtype=bool)
    for value in np.unique(sums):
        i = np.flatnonzero(sums == value)[0]
        reject[sums == value] = c.p_values(errors, c.ASSIGNMENTS[i])[1] <= c.ALPHA
    assert 0 < reject.mean() <= c.ALPHA
