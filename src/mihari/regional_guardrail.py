"""Compare pooled and region-standardized latency guardrails on paired traffic."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
from statistics import mean

from numpy import count_nonzero
from numpy.random import default_rng

from .fixed_steps import ROLLBACK_CUTOFF
from .matched_risk import PairedCounts
from .rollout import GRACE, LOOKS, MIN_CANARY, ONSET, SLOW_MS
from .study import HORIZON, frequency


SEEDS = range(990_000, 992_000)
WINDOW_SECONDS = 30
RECOVERY = ONSET + 4
GAP_CUTOFF = Fraction(str(ROLLBACK_CUTOFF))
REGIONS = ("A", "B")
OFFERED = (250, 750)
TARGET_WEIGHTS = tuple(Fraction(n, sum(OFFERED)) for n in OFFERED)
BASE = (0.07, 0.04)
NULLS = ("balanced-outage", "skewed-outage", "missing-a-control")
FAULTS = {"global-regression": (0, 1), "regional-regression": (0,)}
EXAMPLES = ("skewed-outage", "missing-a-control")
POLICIES = ("pooled", "adjusted")


@dataclass(frozen=True, slots=True)
class Arm:
    sent: int
    seen: int
    slow: int


@dataclass(frozen=True, slots=True)
class Region:
    control: Arm
    canary: Arm


def observe(slow, delays) -> Arm:
    visible = delays <= GRACE
    return Arm(len(slow), int(count_nonzero(visible)), int(count_nonzero(visible & slow)))


def make_windows(case: str, seed: int, *, scripted: bool = False):
    if case not in NULLS and case not in FAULTS:
        raise ValueError(f"Unknown regional scenario: {case}")
    canary_sizes = (25, 75) if case == "balanced-outage" else (70, 30)
    rng = default_rng(seed)
    shape = LOOKS, sum(OFFERED)
    # Draw before slicing by arm so a seed keeps the same per-region inputs across cases.
    load = rng.choice((0.0, 0.0, 0.02, 0.04), LOOKS)
    draws = rng.random(shape)
    delays = rng.integers(0, 4, shape)
    windows = []
    for block in range(LOOKS):
        active = ONSET <= block < RECOVERY
        groups = []
        start = 0
        for i, (offered, canary_n) in enumerate(zip(OFFERED, canary_sizes, strict=True)):
            base = BASE[i] + (0 if scripted else load[block])
            if active and case in NULLS and i == 0:
                base += 0.30
            canary_rate = base
            if active and i in FAULTS.get(case, ()):
                canary_rate += 0.18 if case == "global-regression" else 0.22
            missing_control = active and case == "missing-a-control" and i == 0
            cut, end = start + canary_n, start + offered
            if scripted:
                canary = Arm(canary_n, canary_n, round(canary_n * canary_rate))
                control_n = offered - canary_n
                control = (Arm(control_n, 0, 0) if missing_control else
                           Arm(control_n, control_n, round(control_n * base)))
            else:
                canary = observe(draws[block, start:cut] < canary_rate, delays[block, start:cut])
                control = (Arm(end - cut, 0, 0) if missing_control else
                           observe(draws[block, cut:end] < base, delays[block, cut:end]))
            groups.append(Region(control, canary))
            start = end
        windows.append(tuple(groups))
    return tuple(windows)


def arm_gap(canary: Arm, control: Arm) -> Fraction | None:
    if min(canary.seen, control.seen) < MIN_CANARY:
        return None
    return Fraction(canary.slow, canary.seen) - Fraction(control.slow, control.seen)


def total(arms) -> Arm:
    arms = tuple(arms)
    return Arm(sum(a.sent for a in arms), sum(a.seen for a in arms), sum(a.slow for a in arms))


def gap(regions, *, adjusted: bool) -> Fraction | None:
    if not adjusted:
        return arm_gap(total(r.canary for r in regions), total(r.control for r in regions))
    value = Fraction(0)
    for weight, region in zip(TARGET_WEIGHTS, regions, strict=True):
        component = arm_gap(region.canary, region.control)
        if component is None:
            return None
        value += weight * component
    return value


@dataclass(frozen=True, slots=True)
class Look:
    block: int
    at: int
    gap: Fraction | None
    rollback: bool


def replay(windows, *, adjusted: bool):
    previous_crossed = False
    for block, regions in enumerate(windows):
        value = gap(regions, adjusted=adjusted)
        crossed = value is not None and value > GAP_CUTOFF
        rollback = previous_crossed and crossed
        yield Look(block, block + GRACE, value, rollback)
        if rollback:
            return
        previous_crossed = crossed


@dataclass(frozen=True, slots=True)
class Outcome:
    trigger: int | None
    looks: int
    holds: int
    exposed: int

    @property
    def detected(self):
        return self.trigger is not None and ONSET <= self.trigger < RECOVERY

    @property
    def delay(self):
        return self.trigger + GRACE - ONSET if self.detected else HORIZON


def outcome(windows, case, *, adjusted=False):
    looks = tuple(replay(windows, adjusted=adjusted))
    last = looks[-1]
    # Send tick precedes the look at that tick, and sends can be unobserved.
    exposure = sum(regions[i].canary.sent for regions in windows[ONSET:min(RECOVERY, last.at + 1)]
                   for i in FAULTS.get(case, ()))
    return Outcome(last.block if last.rollback else None, len(looks),
                   sum(look.gap is None for look in looks), exposure)


def pair(case, seed):
    windows = make_windows(case, seed)
    return outcome(windows, case), outcome(windows, case, adjusted=True)


def report(case, pairs):
    fault = case in FAULTS
    label = "detected" if fault else "false rollback"
    counts = PairedCounts.count(tuple(o.detected if fault else o.trigger is not None for o in row)
                               for row in pairs)
    for name, runs, only in zip(POLICIES, zip(*pairs), (counts.errors_only, counts.joint_only), strict=True):
        hits = only + counts.both
        holds = sum(o.holds for o in runs)
        looks = sum(o.looks for o in runs)
        print(f"  {name}: {label} {frequency(hits, counts.total)}; ineligible holds {holds}/{looks} looks")
        if fault:
            early = sum(o.trigger is not None and o.trigger < ONSET for o in runs)
            unrelated = sum(o.trigger is not None and o.trigger >= RECOVERY for o in runs)
            late = sum(o.detected and o.trigger + GRACE >= RECOVERY for o in runs)
            print(f"    missed {counts.total - hits} (pre-fault stops {early}, post-fault evidence stops {unrelated}); "
                  f"detections at/after recovery {late}; mean delay {mean(o.delay for o in runs):.2f} ticks "
                  f"(misses charged {HORIZON}); mean affected-region canary sends {mean(o.exposed for o in runs):.1f}")
    print(f"  Paired {label}: pooled only {counts.errors_only}, adjusted only {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")


def run():
    print(f"Regional latency guardrail; {len(SEEDS):,} paired seeds per case ({SEEDS[0]} through {SEEDS[-1]})")
    print(f"{WINDOW_SECONDS}s windows; slow >{SLOW_MS} ms; pooled vs fixed 25/75 target weights.")
    print("Same illustrative cutoff, not equal-risk or production validation. Target average is not worst-region harm.")
    for case in (*NULLS, *FAULTS):
        print(f"\n{case}")
        report(case, [pair(case, seed) for seed in SEEDS])


def show():
    print("Scripted deadline timeline: control and canary slow/seen/sent per region; gaps in percentage points.")
    for case in EXAMPLES:
        windows = make_windows(case, 0, scripted=True)
        policies = tuple(tuple(replay(windows, adjusted=mode)) for mode in (False, True))
        print(f"\n{case}")
        for block in range(max(map(len, policies))):
            regions = " | ".join(
                f"{name} ctl {r.control.slow}/{r.control.seen}/{r.control.sent}, "
                f"can {r.canary.slow}/{r.canary.seen}/{r.canary.sent}"
                for name, r in zip(REGIONS, windows[block], strict=True)
            )
            cells = []
            for label, looks in zip(POLICIES, policies, strict=True):
                if block >= len(looks):
                    cells.append(f"{label} stopped")
                elif (look := looks[block]).gap is None:
                    cells.append(f"{label} HOLD (ineligible)")
                else:
                    action = " ROLLBACK" if look.rollback else ""
                    cells.append(f"{label} {float(100 * look.gap):+.2f} pp{action}")
            print(f"  send {block:02d}, decide {block + GRACE:02d} | {regions} | {'; '.join(cells)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show the two scripted outage timelines")
    if parser.parse_args().timeline:
        show()
    else:
        run()


if __name__ == "__main__":
    main()
