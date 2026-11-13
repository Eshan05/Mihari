"""Test whether the existing rollback rules have comparable held-out risk."""

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from statistics import mean

from scipy.stats import binomtest

from .guardrail import FAULTS, NULLS, RULES, decisions, make_workload, result
from .rollout import ONSET
from .study import HORIZON, TARGET, frequency


CALIBRATION_TRIALS = 2_000
HOLDOUT_TRIALS = 3_000
SEED_BASE = 10_000
MARGIN = 0.015
CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    cutoff: float
    counts: tuple[int, ...]


def feasible_points(peaks):
    """Enumerate attainable cutoff levels at or below the calibration risk cap."""
    ordered = tuple(sorted(group) for group in peaks)
    levels = {score for group in ordered for score in group if score >= 0}
    for cutoff in sorted(levels | {0.0}):
        counts = tuple(len(group) - bisect_right(group, cutoff) for group in ordered)
        if all(count / len(group) <= TARGET for count, group in zip(counts, ordered, strict=True)):
            yield OperatingPoint(cutoff, counts)


def select_points(peaks):
    sizes = {len(group) for groups in peaks.values() for group in groups}
    if len(sizes) != 1 or sizes == {0}:
        raise ValueError("Calibration groups must be nonempty and have equal sizes")
    reference = next(feasible_points(peaks["errors"]))

    def rank(point):
        worst_gap = max(abs(a - b) for a, b in zip(reference.counts, point.counts, strict=True))
        return worst_gap, -max(point.counts), -point.cutoff

    return {"errors": reference, "joint": min(feasible_points(peaks["joint"]), key=rank)}


def paths(scenario: str, n: int, *, holdout: bool):
    for i in range(n):
        trace = make_workload(scenario, SEED_BASE + 2 * i + int(holdout))
        yield tuple(decisions(trace, ONSET if scenario in FAULTS else None))


def calibrate(n: int):
    peaks = {rule: [] for rule in RULES}
    for scenario in NULLS:
        scores = {rule: [] for rule in RULES}
        for path in paths(scenario, n, holdout=False):
            for rule in RULES:
                candidates = (score for item in path if (score := item.score(rule)) is not None)
                scores[rule].append(max(candidates, default=float("-inf")))
        for rule in RULES:
            peaks[rule].append(scores[rule])
    return select_points(peaks)


@dataclass(frozen=True, slots=True)
class PairedCounts:
    neither: int
    errors_only: int
    joint_only: int
    both: int

    @classmethod
    def count(cls, pairs):
        counts = Counter(pairs)
        return cls(counts[False, False], counts[True, False], counts[False, True], counts[True, True])

    @property
    def total(self):
        return self.neither + self.errors_only + self.joint_only + self.both

    @property
    def difference(self):
        return (self.joint_only - self.errors_only) / self.total

    def interval(self, confidence: float):
        # Delta = P(disagree) * (2 P(joint only | disagree) - 1).
        # Split the error probability between the two binomial intervals.
        level = 1 - (1 - confidence) / 2
        discordant = self.errors_only + self.joint_only
        rate = binomtest(discordant, self.total).proportion_ci(level, method="exact")
        direction = (binomtest(self.joint_only, discordant).proportion_ci(level, method="exact")
                     if discordant else (0, 1))
        bounds = [q * (2 * p - 1) for q in rate for p in direction]
        return min(bounds), max(bounds)


def risks_match(intervals):
    return all(scenario in intervals and -MARGIN < intervals[scenario][0]
               <= intervals[scenario][1] < MARGIN for scenario in NULLS)


def paired_results(scenario: str, n: int, points):
    for path in paths(scenario, n, holdout=True):
        yield tuple(result(path, rule, points[rule].cutoff) for rule in RULES)


def rate_difference(value):
    return f"{100 * value:+.2f} pp"


def write_counts(counts):
    print(f"  Paired outcomes: error-only rule alone {counts.errors_only}, joint rule alone {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")


def run(calibration_trials: int = CALIBRATION_TRIALS, holdout_trials: int = HOLDOUT_TRIALS):
    points = calibrate(calibration_trials)
    print(f"90% control / 10% canary; {calibration_trials:,} calibration trials per null; "
          f"{holdout_trials:,} held-out pairs per scenario")
    print("Joint cutoff minimizes the worst calibration mismatch, with each rule capped at 5% in each null.")
    for rule, point in points.items():
        rates = ", ".join(f"{case} {count / calibration_trials:.2%}"
                          for case, count in zip(NULLS, point.counts, strict=True))
        print(f"  {rule}: cutoff {point.cutoff:.8f}; calibration {rates}")

    intervals = {}
    per_null_confidence = 1 - (1 - CONFIDENCE) / len(NULLS)
    for scenario in NULLS:
        print(f"\n{scenario} / held-out false rollback")
        counts = PairedCounts.count(tuple(item[0] is not None for item in pair)
                                   for pair in paired_results(scenario, holdout_trials, points))
        print(f"  errors: {frequency(counts.errors_only + counts.both, counts.total)}")
        print(f"  joint:  {frequency(counts.joint_only + counts.both, counts.total)}")
        write_counts(counts)
        low, high = intervals[scenario] = counts.interval(per_null_confidence)
        print(f"  Joint minus errors: {rate_difference(counts.difference)} "
              f"(simultaneous 95% interval {rate_difference(low)} to {rate_difference(high)})")

    print(f"\nRisk margin: +/-{100 * MARGIN:g} pp for each null, independent of the 5% target.")
    if risks_match(intervals):
        print("Verdict: matched within the margin in both held-out nulls.")
    else:
        print("Verdict: matched risk not established. Lack of a match is not itself evidence of a difference.")
        print("The fault results below are tradeoffs, not an equal-risk ranking.")

    print("Detection means rollback on a window at or after fault onset, not proof of attribution.")
    for scenario in FAULTS:
        print(f"\n{scenario} / held-out faults")
        pairs = list(paired_results(scenario, holdout_trials, points))
        counts = PairedCounts.count(tuple(item[1] is not None for item in pair) for pair in pairs)
        for column, rule in enumerate(RULES):
            records = [pair[column] for pair in pairs]
            hits = sum(delay is not None for _, delay, _ in records)
            early = sum(trigger is not None and delay is None for trigger, delay, _ in records)
            delay = mean(t if t is not None else HORIZON for _, t, _ in records)
            exposure = mean(n for _, _, n in records)
            print(f"  {rule}: detected {frequency(hits, holdout_trials)}; "
                  f"missed {holdout_trials - hits} ({early} pre-fault stops); "
                  f"mean delay (misses charged {HORIZON} ticks) {delay:.2f}; "
                  f"mean affected-window canary sends {exposure:.1f}")
        write_counts(counts)
        print(f"  Paired detection difference (joint minus errors): {rate_difference(counts.difference)}")
        extra = mean(joint[2] - errors[2] for errors, joint in pairs)
        print(f"  Mean paired canary send difference (joint minus errors): {extra:+.1f}")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    run()


if __name__ == "__main__":
    main()
