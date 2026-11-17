"""Compare error-only and joint error/latency rollback at fixed traffic share."""

import argparse
from collections import Counter
from dataclasses import dataclass
from math import ceil
from random import Random
from statistics import mean

from .rollout import GRACE, LOOKS, MIN_CANARY, ONSET, SLOW_MS, Look, Request, replay
from .study import HORIZON, TARGET, frequency


RULES = ("errors", "joint")
NULLS = ("healthy", "shared-degradation")
FAULTS = ("error-only", "latency-only", "combined")


@dataclass(frozen=True, slots=True)
class Scores:
    look: Look
    errors: float | None
    latency: float | None

    def score(self, rule: str) -> float | None:
        if rule not in RULES:
            raise ValueError(f"Unknown guardrail rule: {rule}")
        if rule == "errors" or self.errors is None:
            return self.errors
        return max(self.errors, self.latency)

    def trigger(self, rule: str, cutoff: float) -> str | None:
        score = self.score(rule)
        if score is None or score <= cutoff:
            return None
        if rule == "errors" or self.latency <= cutoff:
            return "errors"
        return "both" if self.errors > cutoff else "latency"


class Guardrail:
    def __init__(self):
        self.previous: tuple[float, float] | None = None

    def update(self, look: Look) -> Scores:
        if min(look.canary, look.control) < MIN_CANARY:
            self.previous = None
            return Scores(look, None, None)
        errors = look.canary_failures / look.canary - look.control_failures / look.control
        latency = look.canary_slow / look.canary - look.control_slow / look.control
        previous = self.previous
        self.previous = errors, latency
        if previous is None:
            return Scores(look, None, None)
        return Scores(look, min(previous[0], errors), min(previous[1], latency))


def decisions(trace, onset: int | None = None, dispatch=None):
    """Read only the same available counts used by the existing replay."""
    guard = Guardrail()
    for decision in replay(trace, onset, dispatch):
        yield guard.update(decision.look)


def make_workload(scenario: str, seed: int, *, on_canary=None):
    """Optionally report each canary request and its same-draw control outcomes."""
    if scenario not in (*NULLS, *FAULTS):
        raise ValueError(f"Unknown guardrail scenario: {scenario}")
    rng = Random(seed)
    trace = []
    for block in range(LOOKS):
        groups = rng.randint(14, 26)
        load = rng.choice((0, 0, 0.03, 0.06))
        shared = scenario == "shared-degradation" and ONSET <= block < ONSET + 3
        baseline_errors = 0.02 + load + (0.10 if shared else 0)
        baseline_slow = 0.05 + load + (0.18 if shared else 0)
        window = []
        for slot in range(groups * 10):
            canary = slot % 10 == 0
            faulty = canary and block >= ONSET
            errors = baseline_errors + (0.12 if faulty and scenario in ("error-only", "combined") else 0)
            slow = baseline_slow + (0.19 if faulty and scenario in ("latency-only", "combined") else 0)
            error_draw = rng.random()
            # A shared draw sometimes correlates the outcomes without tying their marginals.
            slow_draw = error_draw if rng.random() < 0.55 else rng.random()
            available = block + rng.choice((0, 0, 1, 1, GRACE, GRACE + 1))
            request = Request(block, canary, None, error_draw < errors,
                              170 if slow_draw < slow else 25, available)
            window.append(request)
            if on_canary is not None and canary:
                on_canary(request, error_draw < baseline_errors, slow_draw < baseline_slow)
        trace.append(tuple(window))
    return tuple(trace)


def trials_for(scenario: str, trials: int, *, holdout: bool):
    for trial in range(trials):
        seed = 2 * trial + int(holdout)
        yield tuple(decisions(make_workload(scenario, seed), ONSET if scenario in FAULTS else None))


def fit_cutoffs(trials: int):
    cutoff = dict.fromkeys(RULES, 0.0)
    rank = ceil((1 - TARGET) * trials) - 1
    for scenario in NULLS:
        peaks = {rule: [] for rule in RULES}
        for path in trials_for(scenario, trials, holdout=False):
            for rule in RULES:
                available = (value for item in path if (value := item.score(rule)) is not None)
                peaks[rule].append(max(available, default=float("-inf")))
        for rule in RULES:
            cutoff[rule] = max(cutoff[rule], sorted(peaks[rule])[rank])
    return cutoff


def result(path, rule: str, cutoff: float):
    for item in path:
        if (trigger := item.trigger(rule, cutoff)) is not None:
            look = item.look
            delay = look.at - ONSET if look.block >= ONSET else None
            return trigger, delay, look.exposed
    return None, None, path[-1].look.exposed


def run(trials: int):
    cutoffs = fit_cutoffs(trials)
    print(f"90% control / 10% canary; slow means greater than {SLOW_MS} ms")
    print(f"Calibration: {trials} even-seeded trials per null; holdout: {trials} odd-seeded trials per scenario")
    print("One 5% per-rollout calibration target for each rule; the joint rule calibrates the union.")
    print("Held-out risks need not match. Detection is a rollback on a window at or after fault onset.")
    for scenario in (*NULLS, *FAULTS):
        results = {rule: [] for rule in RULES}
        for path in trials_for(scenario, trials, holdout=True):
            for rule in RULES:
                results[rule].append(result(path, rule, cutoffs[rule]))
        print(f"\n{scenario}")
        for rule, records in results.items():
            print(f"  {rule} (gap cutoff {cutoffs[rule]:.4f})")
            if scenario in NULLS:
                hits = [trigger for trigger, _, _ in records if trigger is not None]
                print(f"    false rollback: {frequency(len(hits), trials)}")
            else:
                hits = [trigger for trigger, delay, _ in records if delay is not None]
                early = sum(trigger is not None and delay is None for trigger, delay, _ in records)
                delay = mean(t if t is not None else HORIZON for _, t, _ in records)
                exposure = mean(count for _, _, count in records)
                print(f"    detected: {frequency(len(hits), trials)}; not detected: {trials - len(hits)} "
                      f"({early} rollbacks on pre-fault windows)")
                print(f"    mean delay charging misses {HORIZON} ticks: {delay:.2f}; "
                      f"mean affected-window canary sends: {exposure:.1f}")
            triggers = Counter(hits)
            print(f"    triggers: errors {triggers['errors']}, latency {triggers['latency']}, both {triggers['both']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=500, help="rollouts per scenario in each split")
    args = parser.parse_args()
    if args.trials < 20:
        parser.error("--trials must be at least 20")
    run(args.trials)


if __name__ == "__main__":
    main()
