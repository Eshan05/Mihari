"""Calibrate and compare rollback rules on disjoint, fixed-share rollouts."""

import argparse
from math import ceil, sqrt
from statistics import mean, median

from .rollout import GRACE, LOOKS, ONSET, RULES, SCENARIOS, make_trace, replay


TARGET = 0.05
HORIZON = LOOKS - 1 + GRACE - ONSET


def traces(scenario: str, trials: int, *, holdout: bool):
    # Even and odd seeds are disjoint for any trial count.
    for i in range(trials):
        yield tuple(replay(make_trace(scenario, 2 * i + int(holdout)),
                           ONSET if scenario == "delayed-regression" else None))


def calibrate(trials: int):
    cutoffs = dict.fromkeys(RULES, 0.0)
    for scenario in SCENARIOS[:2]:
        peaks = {rule: [] for rule in RULES}
        for trace in traces(scenario, trials, holdout=False):
            for rule in RULES:
                scores = [score for decision in trace if (score := decision.score(rule)) is not None]
                peaks[rule].append(max(scores, default=float("-inf")))
        rank = ceil((1 - TARGET) * trials) - 1
        for rule in RULES:
            cutoffs[rule] = max(cutoffs[rule], sorted(peaks[rule])[rank])
    return cutoffs


def wilson(count: int, total: int):
    z = 1.96
    p = count / total
    scale = 1 + z**2 / total
    middle = (p + z**2 / (2 * total)) / scale
    width = z * sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / scale
    return max(0.0, middle - width), min(1.0, middle + width)


def outcome(trace, rule: str, cutoff: float):
    stop = next((item.look for item in trace if item.rollback(rule, cutoff)), None)
    delay = stop.at - ONSET if stop is not None and stop.block >= ONSET else None
    return stop, delay, (stop or trace[-1].look).exposed


def frequency(count: int, total: int) -> str:
    low, high = wilson(count, total)
    return f"{count}/{total} ({count / total:.1%}; 95% CI {low:.1%} to {high:.1%})"


def run(trials: int):
    cutoffs = calibrate(trials)
    print(f"Fixed 90% control / 10% canary; {LOOKS} traffic windows; {GRACE}-tick telemetry grace")
    print(f"Calibration: {trials} rollouts per null scenario; holdout: {trials} per scenario")
    print("Target: no more than 5% false rollbacks per rollout under either calibration null.")
    print("Report shows empirical results, not statistically matched budgets or a policy ranking.")
    for scenario in SCENARIOS:
        results = {rule: [] for rule in RULES}
        for trace in traces(scenario, trials, holdout=True):
            for rule in RULES:
                results[rule].append(outcome(trace, rule, cutoffs[rule]))
        print(f"\n{scenario}")
        for rule, records in results.items():
            print(f"  {rule} (cutoff {cutoffs[rule]:.4f})")
            if scenario != "delayed-regression":
                print(f"    false rollback: {frequency(sum(stop is not None for stop, _, _ in records), trials)}")
                continue
            delays = [delay for _, delay, _ in records if delay is not None]
            early = sum(stop is not None and delay is None for stop, delay, _ in records)
            print(f"    detected: {frequency(len(delays), trials)}; "
                  f"not detected: {trials - len(delays)} ({early} rollbacks on pre-onset windows)")
            detected = f"{median(delays):g} ticks" if delays else "n/a"
            restricted = mean(delay if delay is not None else HORIZON for _, delay, _ in records)
            print(f"    median delay among detections: {detected}; "
                  f"mean delay with misses charged {HORIZON} ticks: {restricted:.2f} ticks")
            print(f"    mean post-onset canary requests before stop or end: "
                  f"{mean(exposed for _, _, exposed in records):.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=1000, help="rollouts per scenario in each split")
    args = parser.parse_args()
    if args.trials < 20:
        parser.error("--trials must be at least 20")
    run(args.trials)


if __name__ == "__main__":
    main()
