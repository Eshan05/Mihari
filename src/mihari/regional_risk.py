"""Compare regional latency triggers under one rollout-level risk budget."""

import argparse
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from statistics import mean

from . import regional_guardrail as regional
from .matched_risk import CONFIDENCE, MARGIN, PairedCounts, rate_difference, select_points
from .rollout import GRACE, ONSET
from .study import HORIZON, TARGET, frequency


CALIBRATION = range(1_010_000, 1_012_000)
NULL_HOLDOUT = range(1_020_000, 1_028_000)
FAULT_HOLDOUT = range(1_040_000, 1_043_000)
BRANCHES = ("overall", *regional.REGIONS)
POLICIES = ("adjusted-only", "adjusted-plus-regional")
EXAMPLES = ("regional-regression", "missing-a-control")
Gaps = tuple[Fraction, ...]


@dataclass(frozen=True, slots=True)
class Look:
    gaps: Gaps | None
    scores: Gaps | None


def score_path(windows):
    previous = None
    for regions in windows:
        components = tuple(regional.arm_gap(r.canary, r.control) for r in regions)
        if any(value is None for value in components):
            yield Look(None, None)
            previous = None
            continue
        overall = sum(weight * value for weight, value in zip(regional.TARGET_WEIGHTS, components, strict=True))
        gaps = (overall, *components)
        scores = tuple(map(min, zip(previous, gaps, strict=True))) if previous is not None else None
        yield Look(gaps, scores)
        previous = gaps


def peaks(path):
    eligible = [look.scores for look in path if look.scores is not None]
    return (max((scores[0] for scores in eligible), default=float("-inf")),
            max((max(scores) for scores in eligible), default=float("-inf")))


def calibrate():
    by_policy = [[], []]
    for case in regional.NULLS:
        traces = (peaks(score_path(regional.make_windows(case, seed))) for seed in CALIBRATION)
        for groups, case_peaks in zip(by_policy, zip(*traces), strict=True):
            groups.append(case_peaks)
    # The shared selector calls its one-branch reference "errors".
    points = select_points({"errors": by_policy[0], "joint": by_policy[1]})
    return points["errors"], points["joint"]


def crossings(look, cutoff, *, joint):
    if look.scores is None:
        return ()
    scores = look.scores if joint else look.scores[:1]
    return tuple(branch for branch, score in zip(BRANCHES, scores) if score > cutoff)


@dataclass(frozen=True, slots=True)
class Result:
    rollout: regional.Outcome
    branches: tuple[str, ...]


def evaluate(windows, path, case, cutoff, *, joint):
    holds = 0
    for block, look in enumerate(path):
        holds += look.gaps is None
        crossed = crossings(look, cutoff, joint=joint)
        if crossed:
            break
    end = min(regional.RECOVERY, block + GRACE + 1)
    exposed = sum(regions[i].canary.sent for regions in windows[ONSET:end] for i in regional.FAULTS.get(case, ()))
    return Result(regional.Outcome(block if crossed else None, block + 1, holds, exposed), crossed)


def pair(case, seed, points):
    windows = regional.make_windows(case, seed)
    path = tuple(score_path(windows))
    return tuple(evaluate(windows, path, case, point.cutoff, joint=bool(index))
                 for index, point in enumerate(points))


def write_calibration(points):
    print(f"Calibration: {len(CALIBRATION):,} pairs per null, seeds {CALIBRATION[0]} through {CALIBRATION[-1]}.")
    print(f"At most {TARGET:.0%} per calibration scenario for each entire rule, not per branch.")
    for name, point in zip(POLICIES, points, strict=True):
        rates = ", ".join(f"{case} {n}/{len(CALIBRATION)}" for case, n in zip(regional.NULLS, point.counts, strict=True))
        print(f"  {name}: strict cutoff {Fraction(point.cutoff)} ({float(100 * point.cutoff):.4f} pp); {rates}")


def status(result, fault):
    return result.rollout.detected if fault else result.rollout.trigger is not None


def report(case, pairs):
    fault = case in regional.FAULTS
    label = "detected" if fault else "false rollback"
    counts = PairedCounts.count(tuple(status(result, fault) for result in row) for row in pairs)
    for index, (name, only) in enumerate(zip(POLICIES, (counts.errors_only, counts.joint_only), strict=True)):
        results = [row[index] for row in pairs]
        runs = [result.rollout for result in results]
        hits = only + counts.both
        print(f"  {name}: {label} {frequency(hits, counts.total)}; "
              f"ineligible holds {sum(o.holds for o in runs)}/{sum(o.looks for o in runs)} looks")
        reasons = Counter("+".join(result.branches) for result in results if result.branches)
        print("    rollback branches: " + (", ".join(f"{names} {n}" for names, n in sorted(reasons.items())) or "none"))
        if fault:
            early = sum(o.trigger is not None and o.trigger < ONSET for o in runs)
            unrelated = sum(o.trigger is not None and o.trigger >= regional.RECOVERY for o in runs)
            late = sum(o.detected and o.trigger + GRACE >= regional.RECOVERY for o in runs)
            print(f"    missed {counts.total - hits} (pre-fault stops {early}, post-fault evidence stops {unrelated}); "
                  f"detections at/after recovery {late}; mean delay {mean(o.delay for o in runs):.2f} ticks "
                  f"(misses charged {HORIZON}); mean affected-region canary sends {mean(o.exposed for o in runs):.1f}")
    print(f"  Paired {label}: adjusted-only rule alone {counts.errors_only}, joint rule alone {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")
    if fault:
        extra = mean(right.rollout.exposed - left.rollout.exposed for left, right in pairs)
        print(f"  Joint minus adjusted-only: detection {rate_difference(counts.difference)}; affected sends {extra:+.1f}")
        return None
    interval = counts.interval(1 - (1 - CONFIDENCE) / len(regional.NULLS))
    low, high = map(rate_difference, interval)
    print(f"  Joint minus adjusted-only: {rate_difference(counts.difference)} "
          f"(simultaneous {CONFIDENCE:.0%} interval {low} to {high})")
    return interval


def risks_match(intervals):
    return all(case in intervals and -MARGIN < intervals[case][0] <= intervals[case][1] < MARGIN
               for case in regional.NULLS)


def run(points):
    for label, seeds in (("null", NULL_HOLDOUT), ("fault", FAULT_HOLDOUT)):
        print(f"Held out: {len(seeds):,} pairs per {label}, seeds {seeds[0]} through {seeds[-1]}.")
    intervals = {}
    for case in regional.NULLS:
        print(f"\n{case} / held-out false rollback")
        intervals[case] = report(case, [pair(case, seed, points) for seed in NULL_HOLDOUT])
    print(f"\nRisk margin: +/-{100 * MARGIN:g} percentage points in all three modeled nulls.")
    if risks_match(intervals):
        print("Verdict: false-rollback risks matched within the margin in all three modeled nulls.")
    else:
        print("Verdict: matched risk not established. Fault results are tradeoffs, not an equal-risk ranking.")
    print(f"This does not establish compliance with a {TARGET:.0%} held-out risk cap or estimate production risk.")
    for case in regional.FAULTS:
        print(f"\n{case} / held-out faults")
        report(case, [pair(case, seed, points) for seed in FAULT_HOLDOUT])


def show(points):
    print("Scripted branch scores are the smaller gap from two consecutive eligible windows, in percentage points.")
    for case in EXAMPLES:
        print(f"\n{case}")
        windows = regional.make_windows(case, 0, scripted=True)
        stopped = [False, False]
        for block, look in enumerate(score_path(windows)):
            eligible = look.gaps is not None
            scores = ("; ".join(f"{name} {rate_difference(float(value))}"
                               for name, value in zip(BRANCHES, look.scores, strict=True)) if look.scores is not None
                      else "waiting for second look" if eligible else "HOLD: ineligible regional evidence")
            actions = []
            for index, (label, point) in enumerate(zip(POLICIES, points, strict=True)):
                result = "stopped" if stopped[index] else "continue" if eligible else "HOLD"
                if not stopped[index] and (crossed := crossings(look, point.cutoff, joint=bool(index))):
                    stopped[index] = True
                    result = f"ROLLBACK on {'+'.join(crossed)}"
                actions.append(f"{label} {result}")
            print(f"  send {block:02d}, decide {block + GRACE:02d} | {scores} | {'; '.join(actions)}")
            if all(stopped):
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show scripted branch decisions at fitted cutoffs")
    args = parser.parse_args()
    points = calibrate()
    write_calibration(points)
    (show if args.timeline else run)(points)


if __name__ == "__main__":
    main()
