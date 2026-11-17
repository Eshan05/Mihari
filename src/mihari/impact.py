"""Count incremental canary harm and compare explicit rollout cost assumptions."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
from statistics import mean

from .guardrail import FAULTS, NULLS, RULES, decisions, make_workload
from .risk_confirmation import FAULT_SEEDS, NULL_SEEDS, frozen_points
from .rollout import GRACE, LOOKS, ONSET, SLOW_MS


CASES = (*NULLS, *FAULTS)
SLOW_WEIGHTS = (Fraction(1, 4), Fraction(1))
STOP_WEIGHTS = (0, 100, 1_000)
MIXES = {"error-heavy": (90, 5, 3, 1, 1), "latency-heavy": (90, 5, 1, 3, 1)}
EXAMPLE_SEED = FAULT_SEEDS.start


@dataclass(frozen=True, slots=True)
class Harm:
    failures: int
    slows: int
    both: int

    @property
    def slow_only(self):
        return self.slows - self.both


def workload(case, seed):
    counts = [[0, 0, 0] for _ in range(LOOKS)]

    def record(request, control_failed, control_slow):
        failure = request.failed and not control_failed
        slow = request.latency_ms > SLOW_MS and not control_slow
        row = counts[request.block]
        row[0] += failure
        row[1] += slow
        row[2] += failure and slow

    trace = make_workload(case, seed, on_canary=record)
    path = tuple(decisions(trace, ONSET if case in FAULTS else None))
    return trace, path, tuple(Harm(*row) for row in counts)


def total_harm(blocks):
    return Harm(sum(h.failures for h in blocks), sum(h.slows for h in blocks), sum(h.both for h in blocks))


@dataclass(frozen=True, slots=True)
class Outcome:
    branch: str | None
    window: int
    at: int
    affected_sends: int
    harm: Harm


def evaluate(path, blocks, rule, cutoff):
    for item in path:
        branch = item.trigger(rule, cutoff)
        if branch is not None:
            break
    look = item.look
    # Dispatch at a tick precedes its look; the ledger includes outcomes absent from that look.
    return Outcome(branch, look.block, look.at, look.exposed, total_harm(blocks[:look.at + 1]))


def pair(case, seed, points):
    _, path, blocks = workload(case, seed)
    return tuple(evaluate(path, blocks, rule, points[rule].cutoff) for rule in RULES)


@dataclass(frozen=True, slots=True)
class CostTerms:
    failures: Fraction
    slow_only: Fraction
    stops: Fraction

    def score(self, slow_weight, stop_weight):
        return self.failures + slow_weight * self.slow_only + stop_weight * self.stops


def summarize(case, runs):
    fault = case in FAULTS
    early = sum(fault and run.branch is not None and run.window < ONSET for run in runs)
    rollback = sum(not fault and run.branch is not None for run in runs)
    detected = sum(fault and run.branch is not None and run.window >= ONSET for run in runs)
    total = total_harm(tuple(run.harm for run in runs))
    n = len(runs)
    print(f"    detected {detected}/{n}; no-version-fault rollbacks {rollback}; pre-fault-evidence stops {early}")
    print(f"    mean affected sends {mean(run.affected_sends for run in runs):.1f}; "
          f"added failures {total.failures / n:.3f}; added slows {total.slows / n:.3f}; "
          f"both {total.both / n:.3f}; added slow only {total.slow_only / n:.3f}")
    return CostTerms(Fraction(total.failures, n), Fraction(total.slow_only, n), Fraction(rollback + early, n))


def mix_terms(terms, weights):
    pairs = tuple(zip(terms, weights, strict=True))
    return CostTerms(
        sum(term.failures * weight for term, weight in pairs),
        sum(term.slow_only * weight for term, weight in pairs),
        sum(term.stops * weight for term, weight in pairs),
    )


def crossover(errors, joint, slow_weight):
    intercept = joint.score(slow_weight, 0) - errors.score(slow_weight, 0)
    slope = joint.stops - errors.stops
    if slope == 0:
        return "tie at every stop weight" if intercept == 0 else "no nonnegative crossover"
    crossing = -intercept / slope
    if crossing == 0:
        return "tie at stop weight zero"
    return f"crossover at stop weight {float(crossing):.2f}" if crossing > 0 else "no nonnegative crossover"


def sensitivity(by_case):
    print("\nHypothetical sensitivity: added-failure-equivalent units per rollout, not dollars.")
    print("One unit per added failure; slow weight applies only to incremental slows not counted as extra failures.")
    print("Stop weight applies to all null rollbacks and fault runs stopped on pre-fault evidence.")
    for name, percentages in MIXES.items():
        print(f"\n{name}: " + ", ".join(f"{case} {p}%" for case, p in zip(CASES, percentages, strict=True)))
        weights = tuple(Fraction(p, 100) for p in percentages)
        errors, joint = tuple(mix_terms((by_case[case][index] for case in CASES), weights)
                              for index in range(len(RULES)))
        for slow in SLOW_WEIGHTS:
            print(f"  slow-only weight {float(slow):g}: {crossover(errors, joint, slow)}")
            for stop in STOP_WEIGHTS:
                base, candidate = errors.score(slow, stop), joint.score(slow, stop)
                print(f"    stop weight {stop:4d}: error-only {float(base):.4f}; joint {float(candidate):.4f}; "
                      f"joint minus error-only {float(candidate - base):+.4f}")


def run(points):
    print("Same frozen confirmation cohort, not independent validation.")
    print("Incremental harm uses same-draw control outcomes; totals are means per rollout.")
    by_case = {}
    for case in CASES:
        seeds = FAULT_SEEDS if case in FAULTS else NULL_SEEDS
        print(f"\n{case}: {len(seeds):,} paired seeds ({seeds.start} through {seeds.stop - 1})")
        pairs = [pair(case, seed, points) for seed in seeds]
        means = []
        for rule, outcomes in zip(RULES, zip(*pairs), strict=True):
            print(f"  {rule}, cutoff {points[rule].cutoff!r}")
            means.append(summarize(case, outcomes))
        by_case[case] = means
    sensitivity(by_case)
    print("\nThese results describe a synthetic cohort and assumed costs, not production impact or savings.")


def show(points):
    case = "combined"
    trace, path, blocks = workload(case, EXAMPLE_SEED)
    runs = {rule: evaluate(path, blocks, rule, points[rule].cutoff) for rule in RULES}
    print(f"{case}, frozen seed {EXAMPLE_SEED}. Send ticks at or before the decision count, even without evidence.")
    for rule, run in runs.items():
        action = f"rollback on {run.branch}, evidence window {run.window}" if run.branch else "no rollback"
        print(f"  {rule}: {action}; last decision tick {run.at}; affected sends {run.affected_sends}; "
              f"added failures {run.harm.failures}, added slows {run.harm.slows}, both {run.harm.both}")
    print("  tick | canary sent | on-time at own deadline | added failures / slows / both | errors / joint")
    for tick, (rows, harm) in enumerate(zip(trace, blocks, strict=True)):
        canary = [r for r in rows if r.canary]
        seen = sum(r.available_at is not None and r.available_at <= tick + GRACE for r in canary)
        included = " / ".join("count" if tick <= run.at else "excluded" for run in runs.values())
        print(f"  {tick:4d} | {len(canary):11d} | {seen:23d} | "
              f"{harm.failures:14d} / {harm.slows:5d} / {harm.both:4d} | {included}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show the preselected paired impact walkthrough")
    args = parser.parse_args()
    points = frozen_points()
    (show if args.timeline else run)(points)


if __name__ == "__main__":
    main()
