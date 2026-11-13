"""Compare prompt and late canary telemetry on paired fixed-share rollouts."""

import argparse
from dataclasses import replace
from random import Random
from statistics import mean

from .rollout import GRACE, ONSET, RULES, make_trace, replay
from .study import HORIZON, calibrate_groups, frequency, outcome


NULLS = ("healthy", "shared-outage")
FAULTS = {"weak-sustained": (0.03, None), "one-window": (0.08, 1)}
PROFILES = ("prompt", "delayed")


def retime(trace, profile: str, seed: int):
    """Change availability only. The delay draw never uses the request outcome."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown telemetry profile: {profile}")
    rng = Random(seed ^ 0xD31A)
    canary_delays = (0, 0, 0, 0, 1, 1, 1, GRACE, GRACE, GRACE + 1)

    def retime_request(row):
        delay = rng.choice(canary_delays) if profile == "delayed" and row.canary else 0
        available = row.block + delay
        return row if row.available_at == available else replace(row, available_at=available)

    return tuple(tuple(retime_request(row) for row in window) for window in trace)


def fault_end(scenario: str):
    if scenario in FAULTS and (duration := FAULTS[scenario][1]) is not None:
        return ONSET + duration
    return None


def simulate(scenario: str, seed: int):
    onset = None
    if scenario in FAULTS:
        increase, duration = FAULTS[scenario]
        trace = make_trace("delayed-regression", seed, increase=increase, duration=duration)
        onset = ONSET
    else:
        trace = make_trace(scenario, seed)
    prompt = retime(trace, "prompt", seed)
    sent = sum(row.canary for window in prompt for row in window)
    runs = {}
    for profile in PROFILES:
        telemetry = prompt if profile == "prompt" else retime(prompt, profile, seed)
        decisions = tuple(replay(telemetry, onset, complete_early=True, fault_end=fault_end(scenario)))
        missing = sent - sum(d.look.canary for d in decisions)
        wait = mean(d.look.at - d.look.block for d in decisions)
        runs[profile] = (decisions, missing, wait)
    return runs


def rollouts(scenario: str, trials: int, *, holdout: bool):
    for i in range(trials):
        yield simulate(scenario, 2 * i + int(holdout))


def cutoffs(trials: int):
    return calibrate_groups((((scenario, profile), decisions)
                             for scenario in NULLS
                             for pair in rollouts(scenario, trials, holdout=False)
                             for profile, (decisions, _, _) in pair.items()), trials)


def run(trials: int):
    frozen = cutoffs(trials)
    print("Fixed 90% control / 10% canary; same sends, outcomes, policy cutoffs and deadline in each pair")
    print(f"Calibration: {trials} even-seeded pairs per null; holdout: {trials} odd-seeded pairs per scenario")
    print("Detection means rollback on a window at or after fault onset, not proof of fault attribution.")
    print("Each rule targets <=5% false rollbacks in calibration; held-out budgets need not match.")
    for scenario in (*NULLS, *FAULTS):
        records = {rule: {profile: [] for profile in PROFILES} for rule in RULES}
        timing = {profile: [] for profile in PROFILES}
        for pair in rollouts(scenario, trials, holdout=True):
            for profile, (decisions, missing, wait) in pair.items():
                timing[profile].append((missing, wait))
                for rule in RULES:
                    records[rule][profile].append(outcome(decisions, rule, frozen[rule]))
        print(f"\n{scenario}")
        for profile, metrics in timing.items():
            print(f"  {profile}: mean canary outcomes excluded at close {mean(missing for missing, _ in metrics):.1f}; "
                  f"mean window close lag {mean(wait for _, wait in metrics):.2f} ticks (full replay)")
        for rule, profiles in records.items():
            print(f"  {rule} (cutoff {frozen[rule]:.4f})")
            for profile, results in profiles.items():
                if scenario in NULLS:
                    print(f"    {profile}: false rollback "
                          f"{frequency(sum(stop is not None for stop, _, _ in results), trials)}")
                    continue
                hits = sum(delay is not None for _, delay, _ in results)
                end = fault_end(scenario)
                late = sum(delay is not None and stop.at >= end for stop, delay, _ in results) if end else None
                pre_fault = sum(stop is not None and delay is None for stop, delay, _ in results)
                delay = mean(d if d is not None else HORIZON for _, d, _ in results)
                exposure = mean(exposed for _, _, exposed in results)
                print(f"    {profile}: detected {frequency(hits, trials)}; not detected {trials - hits} "
                      f"({pre_fault} rollbacks on pre-fault windows); "
                      f"mean delay charging misses {HORIZON} ticks: {delay:.2f}; "
                      f"mean affected-window canary sends: {exposure:.1f}")
                if late is not None:
                    print(f"      {late}/{hits} detections rolled back at or after recovery")
            if scenario in FAULTS:
                prompt, delayed = (profiles[profile] for profile in PROFILES)
                both = prompt_only = delayed_only = 0
                for first, second in zip(prompt, delayed, strict=True):
                    a, b = first[1] is not None, second[1] is not None
                    both += a and b
                    prompt_only += a and not b
                    delayed_only += b and not a
                print(f"    paired detections: both {both}, prompt only {prompt_only}, "
                      f"delayed only {delayed_only}, neither {trials - both - prompt_only - delayed_only}; "
                      f"mean affected-window send difference (delayed minus prompt): "
                      f"{mean(b[2] - a[2] for a, b in zip(prompt, delayed, strict=True)):+.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=500, help="rollout pairs per scenario in each split")
    args = parser.parse_args()
    if args.trials < 20:
        parser.error("--trials must be at least 20")
    run(args.trials)


if __name__ == "__main__":
    main()
