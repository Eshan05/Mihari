"""Compare request- and tenant-level rollback tests at one planned checkpoint."""

import argparse
from collections import Counter
from itertools import combinations

import numpy as np
from numpy.random import default_rng
from scipy.stats import hypergeom

from .study import frequency


TENANTS = 30
CANARY_TENANTS = 3
BLOCKS = 10
ATTEMPTS = 10
REQUESTS = BLOCKS * ATTEMPTS
BASE_ERROR = 0.02
FAULT_ERROR = 0.04
BURST_CHANCE = 0.35
ALPHA = 0.05
FIRST_SEED = 1_210_000
ASSIGNMENTS = np.array(list(combinations(range(TENANTS), CANARY_TENANTS)), dtype=np.intp)


def sample(seed: int, *, bursts: bool, fault: bool) -> tuple[np.ndarray, np.ndarray]:
    rng = default_rng(seed)
    canary = rng.choice(TENANTS, CANARY_TENANTS, replace=False)
    rates = np.full((TENANTS, 1), BASE_ERROR)
    if fault:
        rates[canary] = FAULT_ERROR
    failed_attempts = rng.binomial(ATTEMPTS, rates, size=(TENANTS, BLOCKS))
    if bursts:
        shared = rng.random((TENANTS, BLOCKS)) < BURST_CHANCE
        all_or_none = ATTEMPTS * rng.binomial(1, rates, size=(TENANTS, BLOCKS))
        failed_attempts = np.where(shared, all_or_none, failed_attempts)
    return failed_attempts.sum(axis=1), canary


def p_values(errors: np.ndarray, canary: np.ndarray) -> tuple[float, float]:
    """Evaluate the same error-rate contrast under two assignment assumptions."""
    observed = errors[canary].sum()
    request_p = hypergeom.sf(observed - 1, TENANTS * REQUESTS, errors.sum(),
                             CANARY_TENANTS * REQUESTS)
    # Equal tenant volumes make the rate gap monotone in the canary error count.
    tenant_p = np.mean(errors[ASSIGNMENTS].sum(axis=1) >= observed)
    return float(request_p), float(tenant_p)


def run(trials: int):
    print(f"{trials:,} paired runs per case, seeds starting at {FIRST_SEED:,}. "
          f"One test at alpha {ALPHA:.0%}; {CANARY_TENANTS}/{TENANTS} tenants on canary.")
    for bursts, fault in ((False, False), (True, False), (False, True), (True, True)):
        counts = Counter(
            tuple(p <= ALPHA for p in p_values(*sample(seed, bursts=bursts, fault=fault)))
            for seed in range(FIRST_SEED, FIRST_SEED + trials)
        )
        traffic = "retry-like bursts" if bursts else "independent requests"
        workload = "canary regression" if fault else "no version effect"
        metric = "detections" if fault else "false rollbacks"
        both = counts[True, True]
        request_only, tenant_only = counts[True, False], counts[False, True]
        print(f"\n{traffic} / {workload}:")
        print(f"  request-level {metric}: {frequency(both + request_only, trials)}")
        print(f"  tenant-level {metric}:  {frequency(both + tenant_only, trials)}")
        print(f"  request only {request_only}; tenant only {tenant_only}; "
              f"both {both}; neither {counts[False, False]}")


def show():
    errors = np.full(TENANTS, 2)
    errors[0] += ATTEMPTS
    canary = np.arange(CANARY_TENANTS)
    observed = errors[canary].sum()
    print("A/A example: identical versions, one tenant has ten additional failed attempts.")
    print(f"Canary: {observed}/{CANARY_TENANTS * REQUESTS} errors; "
          f"control: {errors.sum() - observed}/{(TENANTS - CANARY_TENANTS) * REQUESTS} errors.")
    for label, p in zip(("Request-level", "Tenant-level"), p_values(errors, canary), strict=True):
        print(f"{label}: p={p:.5f} -> {'ROLLBACK' if p <= ALPHA else 'NO ROLLBACK'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trials", type=int, default=4000, help="paired runs per case")
    mode.add_argument("--example", action="store_true", help="show one scripted A/A decision")
    args = parser.parse_args()
    if args.example:
        show()
    elif args.trials < 1:
        parser.error("--trials must be positive")
    else:
        run(args.trials)


if __name__ == "__main__":
    main()
