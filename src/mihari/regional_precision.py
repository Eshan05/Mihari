"""Test precision-adjusted regional scores at comparable false-rollback risk."""

import argparse
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from math import sqrt
from statistics import mean

from . import regional_guardrail as regional
from . import regional_risk as raw
from .matched_risk import CONFIDENCE, MARGIN, PairedCounts, rate_difference, select_points
from .rollout import GRACE, MIN_CANARY, ONSET
from .study import HORIZON, TARGET, frequency


CALIBRATION = range(1_050_000, 1_052_000)
NULL_HOLDOUT = range(1_060_000, 1_068_000)
FAULT_HOLDOUT = range(1_080_000, 1_083_000)
RAW_CUTOFFS = (Fraction(68_419, 1_062_600), Fraction(465, 3_268))
POLICIES = ("precision overall", "precision joint", "raw overall", "raw joint")


@dataclass(frozen=True, slots=True)
class Look:
    """Window scores and persisted scores, in the shape used by raw replay."""

    gaps: tuple[float, ...] | None
    scores: tuple[float, ...] | None


def gap_variance(region):
    canary, control = region.canary, region.control
    if min(canary.seen, control.seen) < MIN_CANARY:
        return None
    pooled = (canary.slow + control.slow + 0.5) / (canary.seen + control.seen + 1)
    gap = canary.slow / canary.seen - control.slow / control.seen
    return gap, pooled * (1 - pooled) * (1 / canary.seen + 1 / control.seen)


def precision_gaps(regions):
    components = tuple(map(gap_variance, regions))
    if any(item is None for item in components):
        return None
    deltas, variances = zip(*components, strict=True)
    weights = regional.TARGET_WEIGHTS
    overall = sum(w * gap for w, gap in zip(weights, deltas, strict=True))
    variance = sum(w**2 * v for w, v in zip(weights, variances, strict=True))
    return (overall / sqrt(variance), *(gap / sqrt(v) for gap, v in components))


def score_path(windows):
    previous = None
    for regions in windows:
        current = precision_gaps(regions)
        scores = (tuple(map(min, zip(previous, current, strict=True)))
                  if previous is not None and current is not None else None)
        yield Look(current, scores)
        previous = current


def calibrate():
    by_policy = {"errors": [], "joint": []}
    for case in regional.NULLS:
        trials = (raw.peaks(score_path(regional.make_windows(case, seed))) for seed in CALIBRATION)
        for groups, peaks in zip(by_policy.values(), zip(*trials, strict=True), strict=True):
            groups.append(peaks)
    points = select_points(by_policy)
    return points["errors"], points["joint"]


def pair(case, seed, points):
    windows = regional.make_windows(case, seed)
    adjusted_path = tuple(score_path(windows))
    raw_path = tuple(raw.score_path(windows))
    return tuple(raw.evaluate(windows, path, case, cutoff, joint=bool(joint))
                 for path, cutoffs in ((adjusted_path, tuple(p.cutoff for p in points)),
                                       (raw_path, RAW_CUTOFFS))
                 for joint, cutoff in enumerate(cutoffs))


def report(case, pairs):
    fault = case in regional.FAULTS
    label = "detected" if fault else "false rollback"
    for name, results in zip(POLICIES, zip(*pairs, strict=True), strict=True):
        runs = [result.rollout for result in results]
        hits = sum(raw.status(result, fault) for result in results)
        print(f"  {name}: {label} {frequency(hits, len(runs))}; "
              f"ineligible holds {sum(o.holds for o in runs)}/{sum(o.looks for o in runs)} looks")
        branches = Counter("+".join(result.branches) for result in results if result.branches)
        print("    rollback branches: " + (", ".join(f"{b} {n}" for b, n in sorted(branches.items())) or "none"))
        if fault:
            early = sum(o.trigger is not None and o.trigger < ONSET for o in runs)
            unrelated = sum(o.trigger is not None and o.trigger >= regional.RECOVERY for o in runs)
            late = sum(o.detected and o.trigger + GRACE >= regional.RECOVERY for o in runs)
            print(f"    missed {len(runs) - hits} (pre-fault stops {early}, post-fault evidence stops {unrelated}); "
                  f"detections at/after recovery {late}; mean delay {mean(o.delay for o in runs):.2f} ticks "
                  f"(misses charged {HORIZON}); mean affected-region canary sends {mean(o.exposed for o in runs):.1f}")

    counts = PairedCounts.count((raw.status(left, fault), raw.status(right, fault)) for left, right, *_ in pairs)
    print(f"  Paired precision {label}: overall alone {counts.errors_only}, joint alone {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")
    delta = rate_difference(counts.difference)
    if fault:
        extra = mean(right.rollout.exposed - left.rollout.exposed for left, right, *_ in pairs)
        print(f"  Precision joint minus overall: detection {delta}; affected sends {extra:+.1f}")
        return None
    interval = counts.interval(1 - (1 - CONFIDENCE) / len(regional.NULLS))
    low, high = map(rate_difference, interval)
    print(f"  Precision joint minus overall: {delta} (simultaneous {CONFIDENCE:.0%} interval {low} to {high})")
    return interval


def run(points):
    for label, seeds in (("null", NULL_HOLDOUT), ("fault", FAULT_HOLDOUT)):
        print(f"Held out: {len(seeds):,} pairs per {label}, seeds {seeds[0]} through {seeds[-1]}.")
    print("Frozen raw cutoffs (descriptive controls): " + ", ".join(map(str, RAW_CUTOFFS)))
    intervals = {}
    for case in regional.NULLS:
        print(f"\n{case} / held-out false rollback")
        intervals[case] = report(case, [pair(case, seed, points) for seed in NULL_HOLDOUT])

    print(f"\nRisk margin: +/-{100 * MARGIN:g} percentage points in all three modeled nulls.")
    if raw.risks_match(intervals):
        print("Verdict: precision rules had comparable false-rollback risk in all three modeled nulls.")
    else:
        print("Verdict: comparable risk not established. Fault results are tradeoffs, not an equal-risk ranking.")
    print(f"This does not establish a {TARGET:.0%} held-out risk cap or anytime validity.")
    for case in regional.FAULTS:
        print(f"\n{case} / held-out faults")
        report(case, [pair(case, seed, points) for seed in FAULT_HOLDOUT])


def show(points):
    print("Same raw gap, different sample sizes:")
    for n in (20, 200):
        gap, variance = gap_variance(regional.Region(regional.Arm(n, n, n // 10),
                                                     regional.Arm(n, n, n // 5)))
        print(f"  {n:3} observations per arm | {rate_difference(gap)} raw | {gap / sqrt(variance):.2f} standardized")
    print("Persistence scores take the lower standardized value from the last two eligible adjacent windows.")
    for case in raw.EXAMPLES:
        print(f"\n{case}")
        windows = regional.make_windows(case, 0, scripted=True)
        stopped = [False, False]
        for block, look in enumerate(score_path(windows)):
            eligible = look.gaps is not None
            if not eligible:
                scores = "ineligible regional evidence (reset)"
            elif look.scores is None:
                scores = "waiting for second look"
            else:
                scores = "; ".join(f"{name} {value:+.2f}"
                                   for name, value in zip(raw.BRANCHES, look.scores, strict=True))
            actions = []
            for index, point in enumerate(points):
                if stopped[index]:
                    status = "stopped"
                elif not eligible:
                    status = "HOLD"
                elif crossed := raw.crossings(look, point.cutoff, joint=bool(index)):
                    status = f"ROLLBACK on {'+'.join(crossed)}"
                    stopped[index] = True
                else:
                    status = "continue"
                actions.append(f"{POLICIES[index]} {status}")
            print(f"  send {block:02d}, decide {block + GRACE:02d} | {scores} | {'; '.join(actions)}")
            if all(stopped):
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show scripted decisions at the fitted cutoffs")
    args = parser.parse_args()
    points = calibrate()
    print(f"Calibration: {len(CALIBRATION):,} pairs per null, seeds {CALIBRATION[0]} through {CALIBRATION[-1]}.")
    print(f"At most {TARGET:.0%} per calibration null for each whole rule across all looks and branches.")
    for name, point in zip(POLICIES[:2], points, strict=True):
        counts = ", ".join(f"{case} {n}/{len(CALIBRATION)}" for case, n in zip(regional.NULLS, point.counts, strict=True))
        print(f"  {name}: strict cutoff {point.cutoff:.17g}; {counts}")
    (show if args.timeline else run)(points)


if __name__ == "__main__":
    main()
