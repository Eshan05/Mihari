"""Compare bounded state-space allocation and smoothing on canary traffic."""

import argparse
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import mean, stdev

import numpy as np
from scipy.stats import t


PERIODS = 32
REQUESTS = 60
BURN_IN = 4
FIXED = 15
FLOOR, CAP = 6, 30
SHIFT_PERIOD, SHIFT_MS = 17, 25.0
SEEDS = range(1_120_000, 1_121_500)
POLICIES = ("fixed", "neyman", "state-space")
SCENARIOS = ("matched", "level-shift")


@dataclass(frozen=True, slots=True)
class ArmModel:
    beta: float
    innovation_var: float
    prior_mean: float
    prior_var: float

    @property
    def information(self):
        return (1 - self.beta)**2 / self.innovation_var


MODELS = (ArmModel(0.90, 100, 0, 144), ArmModel(0.65, 4, 18, 64))
TRUE_SD = (30, 35)


def valid_variance(values):
    return values is not None and all(v > 0 and isfinite(v) for v in values)


def objective(count, variance, information):
    """Steady-state error variance (eq. 28); zero information gives Neyman."""
    control = variance[0] / (REQUESTS - count + variance[0] * information[0])
    canary = variance[1] / (count + variance[1] * information[1])
    return control + canary


def choose_count(policy, period, prior):
    if policy not in POLICIES:
        raise ValueError(f"Unknown allocation: {policy}")
    if period <= BURN_IN or policy == "fixed":
        return FIXED, False
    if not valid_variance(prior):
        return FIXED, True
    information = tuple(model.information for model in MODELS) if policy == "state-space" else (0, 0)
    # Increasing count order breaks equal objective values toward less exposure.
    count = min(range(FLOOR, CAP + 1), key=lambda n: objective(n, prior, information))
    return count, False


@dataclass(frozen=True, slots=True)
class Tape:
    latent: np.ndarray
    potential: np.ndarray
    ordering: np.ndarray


def make_tape(seed, scenario):
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown state-space scenario: {scenario}")
    rng = np.random.default_rng(seed)
    latent = np.empty((PERIODS, 2))
    for arm, model in enumerate(MODELS):
        state = rng.normal(model.prior_mean, sqrt(model.prior_var))
        for i, innovation in enumerate(rng.normal(0, sqrt(model.innovation_var), PERIODS)):
            state = model.beta * state + innovation
            latent[i, arm] = state
    if scenario == "level-shift":
        latent[SHIFT_PERIOD - 1:, 1] += SHIFT_MS
    noises = rng.normal(size=(PERIODS, 2, REQUESTS)) * np.array(TRUE_SD)[None, :, None]
    ordering = np.array([rng.permutation(REQUESTS) for _ in range(PERIODS)])
    return Tape(latent, latent[:, :, None] + noises, ordering)


@dataclass(frozen=True, slots=True)
class Period:
    canary: int
    means: tuple[float, float]
    prior: tuple[float, float] | None
    fallback: bool

    @property
    def gap(self):
        return self.means[1] - self.means[0]


@dataclass(frozen=True, slots=True)
class Observations:
    periods: tuple[Period, ...]
    variance: tuple[float, float]


def observe(tape, policy):
    residual_ss = [0.0, 0.0]
    df = [0, 0]
    periods = []

    def pooled():
        return tuple(ss / n for ss, n in zip(residual_ss, df, strict=True))

    for i, (potential, ordering) in enumerate(zip(tape.potential, tape.ordering, strict=True), 1):
        prior = pooled() if all(df) else None
        count, fallback = choose_count(policy, i, prior)
        means = []
        for arm, indices in enumerate((ordering[count:], ordering[:count])):
            observed = potential[arm, indices]
            avg = float(observed.mean())
            residual = observed - avg
            means.append(avg)
            residual_ss[arm] += float(residual @ residual)
            df[arm] += len(observed) - 1
        periods.append(Period(count, tuple(means), prior, fallback))
    return Observations(tuple(periods), pooled())


def smooth(observed, observation_var, model):
    """Scalar Kalman update and RTS backward pass (eqs. 12-20)."""
    mean_hat, variance = model.prior_mean, model.prior_var
    predicted, filtered = [], []
    for value, r in zip(observed, observation_var, strict=True):
        forecast = model.beta * mean_hat
        forecast_var = model.beta**2 * variance + model.innovation_var
        gain = forecast_var / (forecast_var + r)
        mean_hat = forecast + gain * (value - forecast)
        variance = forecast_var * (r / (forecast_var + r))
        predicted.append((forecast, forecast_var))
        filtered.append((mean_hat, variance))

    smoothed = filtered.copy()
    for i in range(len(filtered) - 2, -1, -1):
        current, current_var = filtered[i]
        forecast, forecast_var = predicted[i + 1]
        following, following_var = smoothed[i + 1]
        gain = current_var * model.beta / forecast_var
        smoothed[i] = (current + gain * (following - forecast),
                       current_var + gain**2 * (following_var - forecast_var))
    return np.array(smoothed)


def estimates(observed):
    if not valid_variance(observed.variance):
        raise ValueError("Cannot estimate from nonpositive or nonfinite arm variance")
    if not all(isfinite(period.gap) for period in observed.periods):
        raise ValueError("Cannot estimate from nonfinite period means")
    canary = np.array([period.canary for period in observed.periods])
    counts = (REQUESTS - canary, canary)
    by_arm = []
    for arm, model in enumerate(MODELS):
        means = [period.means[arm] for period in observed.periods]
        states = smooth(means, observed.variance[arm] / counts[arm], model)
        by_arm.append(float(states[:, 0].mean()))
    return mean(period.gap for period in observed.periods), by_arm[1] - by_arm[0]


@dataclass(frozen=True, slots=True)
class Result:
    raw_error: float
    smooth_error: float
    canary_sends: int
    positive_gap_exposure: float
    floor_hits: int
    cap_hits: int
    fallbacks: int


def evaluate(tape, observed):
    truth = tape.latent[:, 1] - tape.latent[:, 0]
    raw, smoothed = estimates(observed)
    target = float(truth.mean())
    counts = [period.canary for period in observed.periods]
    return Result(raw - target, smoothed - target, sum(counts),
                  float(np.maximum(truth, 0) @ counts),
                  counts[BURN_IN:].count(FLOOR), counts[BURN_IN:].count(CAP),
                  sum(period.fallback for period in observed.periods))


def mse_interval(classical, state_space):
    paired = [new.smooth_error**2 - old.smooth_error**2
              for old, new in zip(classical, state_space, strict=True)]
    center = mean(paired)
    # Two fixed contrasts, one per regime, in a simultaneous 95% family.
    critical = t.ppf(1 - 0.05 / (2 * len(SCENARIOS)), len(paired) - 1)
    radius = critical * stdev(paired) / sqrt(len(paired))
    return center, center - radius, center + radius


def run():
    print(f"{len(SEEDS):,} paired runs per regime, seeds {SEEDS[0]}-{SEEDS[-1]}; "
          f"{PERIODS} periods of {REQUESTS} requests.")
    print(f"All rules start {FIXED}/{REQUESTS} for {BURN_IN} periods; "
          f"adaptive counts bounded at {FLOOR}-{CAP}.")
    for scenario in SCENARIOS:
        print(f"\n{scenario}" + (f" (+{SHIFT_MS:g} ms from period {SHIFT_PERIOD})" if scenario == "level-shift" else ""))
        results = {policy: [] for policy in POLICIES}
        for seed in SEEDS:
            tape = make_tape(seed, scenario)
            for policy in POLICIES:
                results[policy].append(evaluate(tape, observe(tape, policy)))
        for policy, rows in results.items():
            print(f"  {policy}:")
            for label, errors in (("plain", [r.raw_error for r in rows]), ("smooth", [r.smooth_error for r in rows])):
                print(f"    {label:6} bias {mean(errors):+.3f} ms; RMSE {sqrt(mean(e**2 for e in errors)):.3f} ms")
            opportunities = len(rows) * (PERIODS - BURN_IN)
            print(f"    mean canary sends {mean(r.canary_sends for r in rows):.1f}; "
                  f"modeled positive-gap exposure {mean(r.positive_gap_exposure for r in rows):.1f} request-ms")
            if policy != "fixed":
                print(f"    post-burn floor {sum(r.floor_hits for r in rows)}/{opportunities}; "
                      f"cap {sum(r.cap_hits for r in rows)}/{opportunities}; "
                      f"fallback {sum(r.fallbacks for r in rows)}/{opportunities}")
        delta, low, high = mse_interval(results["neyman"], results["state-space"])
        print(f"  State-space minus Neyman smoother MSE: {delta:+.3f} ms^2 "
              f"(simultaneous 95% interval {low:+.3f} to {high:+.3f}).")
    print("\nExposure is modeled, not measured harm. This benchmark does not test rollback safety or interval coverage.")


def show():
    seed = SEEDS[0]
    tape = make_tape(seed, "level-shift")
    replays = [observe(tape, policy) for policy in POLICIES]
    print(f"Level shift +{SHIFT_MS:g} ms at period {SHIFT_PERIOD}; frozen seed {seed}.")
    print("Counts are chosen before each period. Gaps are observed afterward.")
    print("Prior SDs use completed periods only, in control/canary order.")
    for i, rows in enumerate(zip(*(replay.periods for replay in replays), strict=True), 1):
        cells = []
        for policy, row in zip(POLICIES, rows, strict=True):
            cell = f"{policy} {row.canary:2}/{REQUESTS} gap {row.gap:+6.1f}"
            if policy != "fixed":
                if i <= BURN_IN:
                    cell += " (burn-in)"
                elif row.fallback:
                    cell += " (fallback)"
                else:
                    cell += f" (SD {sqrt(row.prior[0]):.1f}/{sqrt(row.prior[1]):.1f})"
            cells.append(cell)
        print(f"  {i:02d} | " + " | ".join(cells))
    print(f"Average latent gap (simulator truth only): {(tape.latent[:, 1] - tape.latent[:, 0]).mean():+.2f} ms.")
    for policy, replay in zip(POLICIES, replays, strict=True):
        plain, smoothed = estimates(replay)
        print(f"  {policy}: plain {plain:+.2f} ms; retrospective smooth {smoothed:+.2f} ms.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show the frozen level-shift allocation trace")
    (show if parser.parse_args().timeline else run)()


if __name__ == "__main__":
    main()
