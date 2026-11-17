"""Confirm fixed-share guardrail risk on a fresh, predeclared simulation."""

import argparse
from statistics import mean

from .guardrail import FAULTS, NULLS, RULES, decisions, make_workload, result
from .matched_risk import (
    CALIBRATION_TRIALS, CONFIDENCE, MARGIN, PairedCounts, calibrate,
    rate_difference, risks_match, write_counts,
)
from .rollout import ONSET
from .study import HORIZON, frequency


NULL_SEEDS = range(900_000, 912_000)
FAULT_SEEDS = range(950_000, 953_000)
REFERENCE = {"errors": (0.1062618595825427, (43, 100)), "joint": (0.14304165321278656, (39, 100))}


def frozen_points():
    points = calibrate(CALIBRATION_TRIALS)
    for rule in RULES:
        point = points[rule]
        if (point.cutoff, point.counts) != REFERENCE[rule]:
            raise ValueError(f"The original {rule} calibration did not reproduce.")
    return points


def paired_results(scenario, seeds, points):
    onset = ONSET if scenario in FAULTS else None
    for seed in seeds:
        path = tuple(decisions(make_workload(scenario, seed), onset))
        yield tuple(result(path, rule, points[rule].cutoff) for rule in RULES)


def report_null(scenario, paired):
    counts = PairedCounts.count(tuple(trigger is not None for trigger, _, _ in pair) for pair in paired)
    print(f"\n{scenario} / fresh false rollback")
    print(f"  errors: {frequency(counts.errors_only + counts.both, counts.total)}")
    print(f"  joint:  {frequency(counts.joint_only + counts.both, counts.total)}")
    write_counts(counts)
    interval = counts.interval(1 - (1 - CONFIDENCE) / len(NULLS))
    low, high = map(rate_difference, interval)
    print(f"  Joint minus errors: {rate_difference(counts.difference)} "
          f"(simultaneous {CONFIDENCE:.0%} interval {low} to {high})")
    return interval


def report_fault(scenario, paired):
    pairs = list(paired)
    counts = PairedCounts.count(tuple(delay is not None for _, delay, _ in pair) for pair in pairs)
    print(f"\n{scenario} / fresh faults")
    for column, rule in enumerate(RULES):
        records = [pair[column] for pair in pairs]
        hits = sum(delay is not None for _, delay, _ in records)
        early = sum(trigger is not None and delay is None for trigger, delay, _ in records)
        restricted = mean(HORIZON if delay is None else delay for _, delay, _ in records)
        exposure = mean(sent for _, _, sent in records)
        print(f"  {rule}: detected {frequency(hits, counts.total)}; "
              f"missed {counts.total - hits} ({early} pre-fault stops); "
              f"mean delay (misses charged {HORIZON} ticks) {restricted:.2f}; "
              f"mean affected-window canary sends {exposure:.1f}")
    write_counts(counts)
    print(f"  Paired detection difference (joint minus errors): {rate_difference(counts.difference)}")
    extra = mean(joint[2] - errors[2] for errors, joint in pairs)
    print(f"  Mean paired canary send difference (joint minus errors): {extra:+.1f}")


def run():
    print("90% control / 10% canary; no pooling with the previous holdout.")
    for group, seeds in (("null", NULL_SEEDS), ("fault", FAULT_SEEDS)):
        print(f"  {len(seeds):,} pairs per {group}; seeds {seeds[0]} through {seeds[-1]} inclusive")
    points = frozen_points()
    print(f"Original calibration reproduced: {CALIBRATION_TRIALS:,} trials per null.")
    for rule, point in points.items():
        print(f"  {rule}: cutoff {point.cutoff!r}; calibration counts "
              + ", ".join(f"{case} {count}" for case, count in zip(NULLS, point.counts, strict=True)))

    intervals = {scenario: report_null(scenario, paired_results(scenario, NULL_SEEDS, points))
                 for scenario in NULLS}
    print(f"\nRisk margin: +/-{100 * MARGIN:g} percentage points in both modeled nulls.")
    if risks_match(intervals):
        print("Verdict: false-rollback risks matched within the margin in both modeled nulls.")
    else:
        print("Verdict: matched risk not established. This alone does not establish different risks.")
        print("Fault results show tradeoffs, not an equal-risk ranking.")
    print("This does not establish compliance with a 5% held-out risk cap or estimate production risk.")
    print("Detection means rollback on a window at or after fault onset, not proof of attribution.")
    for scenario in FAULTS:
        report_fault(scenario, paired_results(scenario, FAULT_SEEDS, points))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        run()
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
