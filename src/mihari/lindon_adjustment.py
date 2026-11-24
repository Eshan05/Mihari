"""Compare Lindon's asymptotic t sequence with and without a pre-routing covariate."""

import argparse
from dataclasses import dataclass
from math import expm1, inf, isfinite, log, log1p, sqrt
from statistics import mean

import numpy as np

from .matched_risk import CONFIDENCE, MARGIN, PairedCounts, rate_difference
from .study import frequency


ALPHA = 0.05
G = 1_000
LOOKS = 20
PER_LOOK = 200
CANARY_SHARE = 0.1
REGRESSION_MS = 12.0
NULL_SEEDS = range(1_090_000, 1_092_000)
FAULT_SEEDS = range(1_100_000, 1_102_000)
RULES = ("unadjusted", "adjusted")
SCENARIOS = (("informative", True), ("irrelevant", False))


def log_evidence(n, columns, q, *, g=G):
    """Scalar version of eq. (22); q is the squared robust Wald statistic."""
    nu = n - columns
    a = g / (g + n)
    x = q / nu
    return 0.5 * log(a) + 0.5 * (nu + 1) * (log1p(x) - log1p(a * x))


def critical_radius(n, columns, *, alpha=ALPHA, g=G):
    """Multiply by the robust SE to get the half-width in outcome units."""
    nu = n - columns
    if nu <= 0:
        return inf
    log_a = -log1p(n / g)
    log_b = (2 * log(alpha) + log_a) / (nu + 1)
    if log_b <= log_a:
        return inf
    # Invert the log-evidence threshold without subtracting nearly equal numbers.
    return sqrt(nu * -expm1(log_b) / ((g / (g + n)) * expm1(log_b - log_a)))


def treatment_fit(design, latency):
    """Return the treatment coefficient and HC1 SE, or None if not estimable."""
    n, columns = design.shape
    if n <= columns or not (np.isfinite(design).all() and np.isfinite(latency).all()):
        return None
    try:
        beta, _, rank, _ = np.linalg.lstsq(design, latency, rcond=None)
        if rank != columns:
            return None
        contrast = np.zeros(columns)
        contrast[1] = 1
        direction = np.linalg.solve(design.T @ design, contrast)
    except np.linalg.LinAlgError:
        return None
    residual = latency - np.einsum("ij,j->i", design, beta)
    # An exact fit can still have rounding-sized nonzero residuals.
    if np.max(np.abs(residual)) <= 8 * np.finfo(float).eps * np.max(np.abs(latency)):
        return None
    weighted = residual * np.einsum("ij,j->i", design, direction)
    variance = float(weighted @ weighted) * n / (n - columns)
    if not isfinite(variance) or variance <= 0 or not isfinite(beta[1]):
        return None
    return float(beta[1]), sqrt(variance)


@dataclass(frozen=True, slots=True)
class Assessment:
    effect: float | None = None
    se: float | None = None
    lower: float | None = None

    @property
    def action(self):
        if self.lower is None:
            return "HOLD"
        return "ROLLBACK" if self.lower > 0 else "continue"


def assess(design, latency):
    fit = treatment_fit(design, latency)
    if fit is None:
        return Assessment()
    effect, se = fit
    radius = critical_radius(*design.shape)
    lower = effect - radius * se
    return Assessment(effect, se, lower if isfinite(lower) else None)


@dataclass(frozen=True, slots=True)
class Workload:
    treatment: np.ndarray
    covariate: np.ndarray
    latency: np.ndarray


def make_workload(seed, *, informative, effect):
    rng = np.random.default_rng(seed)
    n = LOOKS * PER_LOOK
    latency_class = rng.integers(0, 2, n)
    unrelated = rng.integers(0, 2, n)
    noise = rng.uniform(-1, 1, n)
    treatment = rng.random(n) < CANARY_SHARE
    latency = 180 + 120 * latency_class + (12 + 24 * latency_class) * noise + effect * treatment
    return Workload(treatment, latency_class if informative else unrelated, latency)


@dataclass(frozen=True, slots=True)
class Look:
    tick: int
    samples: int
    canary_sends: int
    rules: tuple[Assessment | None, ...]


def replay(workload):
    n = len(workload.latency)
    design = np.column_stack((np.ones(n), workload.treatment, workload.covariate))
    stops = [False, False]
    sends = 0
    for tick, end in enumerate(range(PER_LOOK, n + 1, PER_LOOK), 1):
        sends += int(np.count_nonzero(workload.treatment[end - PER_LOOK:end]))
        assessments = []
        for i in range(len(RULES)):
            result = None if stops[i] else assess(design[:end, :i + 2], workload.latency[:end])
            assessments.append(result)
            if result is not None and result.action == "ROLLBACK":
                stops[i] = True
        yield Look(tick, end, sends, tuple(assessments))
        if all(stops):
            break


@dataclass(frozen=True, slots=True)
class Result:
    stop: int | None
    canary_sends: int

    @property
    def stopped(self):
        return self.stop is not None

    @property
    def time(self):
        return self.stop if self.stopped else LOOKS + 1


def run_pair(workload):
    results = [None, None]
    sends = 0
    for look in replay(workload):
        sends = look.canary_sends
        for i, assessment in enumerate(look.rules):
            if assessment is not None and assessment.action == "ROLLBACK":
                results[i] = Result(look.tick, sends)
    return tuple(result if result is not None else Result(None, sends) for result in results)


def report_pairs(pairs, *, fault):
    label = "detected" if fault else "false rollback"
    counts = PairedCounts.count((left.stopped, right.stopped) for left, right in pairs)
    for name, results in zip(RULES, zip(*pairs, strict=True), strict=True):
        hits = sum(r.stopped for r in results)
        print(f"  {name}: {label} {frequency(hits, counts.total)}")
        if fault:
            print(f"    missed {counts.total - hits}; mean look to stop {mean(r.time for r in results):.2f}; "
                  f"mean canary sends {mean(r.canary_sends for r in results):.1f}")
    print(f"  Paired: unadjusted alone {counts.errors_only}, adjusted alone {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")
    delta = rate_difference(counts.difference)
    if fault:
        time = mean(right.time - left.time for left, right in pairs)
        sends = mean(right.canary_sends - left.canary_sends for left, right in pairs)
        print(f"  Adjusted minus unadjusted: detection {delta}; time {time:+.2f} looks; canary sends {sends:+.1f}")
        return None
    interval = counts.interval(1 - (1 - CONFIDENCE) / 2)
    low, high = map(rate_difference, interval)
    print(f"  Adjusted minus unadjusted: {delta} (simultaneous {CONFIDENCE:.0%} interval {low} to {high})")
    return interval


def run():
    print(f"{LOOKS} looks of {PER_LOOK} complete independent requests; {CANARY_SHARE:.0%} canary assignment.")
    print(f"Both rules: alpha={ALPHA:g}, g={G}, same asymptotic t construction. No fitted cutoffs.")
    print(f"Null seeds {NULL_SEEDS[0]}-{NULL_SEEDS[-1]}; fault seeds {FAULT_SEEDS[0]}-{FAULT_SEEDS[-1]}.")
    intervals = []
    for name, informative in SCENARIOS:
        print(f"\n{name} covariate / null")
        pairs = [run_pair(make_workload(s, informative=informative, effect=0)) for s in NULL_SEEDS]
        intervals.append(report_pairs(pairs, fault=False))
    print(f"\nRisk margin: +/-{100 * MARGIN:g} pp in both nulls.")
    if all(-MARGIN < lo <= hi < MARGIN for lo, hi in intervals):
        print("Verdict: false-rollback risk comparable in both modeled nulls.")
    else:
        print("Verdict: comparable risk not established; fault results are not an equal-risk ranking.")
    print(f"Fault: +{REGRESSION_MS:g} ms from first canary request; misses charged look {LOOKS + 1}.")
    for name, informative in SCENARIOS:
        print(f"\n{name} covariate / regression")
        pairs = [run_pair(make_workload(s, informative=informative, effect=REGRESSION_MS)) for s in FAULT_SEEDS]
        report_pairs(pairs, fault=True)
    print("\nCanary sends measure exposure, not customer harm. No exact finite-sample or production guarantee.")


def show():
    seed = FAULT_SEEDS[0]
    workload = make_workload(seed, informative=True, effect=REGRESSION_MS)
    print(f"Informative covariate / +{REGRESSION_MS:g} ms latency / seed {seed}")
    print("Both rules use the same complete request prefix; effects, SEs and bounds are in ms.")
    for look in replay(workload):
        cells = []
        for name, result in zip(RULES, look.rules, strict=True):
            if result is None:
                label = "stopped"
            elif result.effect is None:
                label = "HOLD (not estimable)"
            else:
                bound = f"{result.lower:+.2f}" if result.lower is not None else "unbounded"
                label = f"{result.effect:+.2f} (SE {result.se:.2f}, lower {bound}) {result.action}"
            cells.append(f"{name} {label}")
        print(f"  look {look.tick:02d}, n={look.samples:4}, canary sends={look.canary_sends:3} | {' | '.join(cells)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show both rules on the frozen rollout")
    (show if parser.parse_args().timeline else run)()


if __name__ == "__main__":
    main()
