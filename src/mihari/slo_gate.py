"""Compare checkpoint-only health checks with an early SLO burn-rate hold."""

import argparse
from collections import Counter
from functools import cache
from statistics import mean

from numpy.random import default_rng

from . import fixed_steps as fixed


SHORT = 2
LONG = 4
BURN_LIMIT = 2
ERROR_BUDGET = 0.04
SLOW_BUDGET = 0.075
FIRST_SEED = 1_160_000
SCENARIOS = ("healthy", "sustained-shared", "recovered-shared", "late-shared", "canary-only")
POLICIES = ("checkpoint only", "checkpoint + SLO gate")


def health_hold(closed: tuple[fixed.Evidence, ...]) -> str | None:
    long = tuple(window.control for window in closed[-LONG:])
    if len(long) < LONG or any(arm.seen == 0 or arm.seen != arm.sent for arm in long):
        return None
    recent, history = sum(long[-SHORT:], fixed.Arm()), sum(long, fixed.Arm())
    for metric, short_hits, long_hits, budget in (
        ("errors", recent.errors, history.errors, ERROR_BUDGET),
        ("latency", recent.slow, history.slow, SLOW_BUDGET),
    ):
        short_burn = short_hits / recent.seen / budget
        long_burn = long_hits / history.seen / budget
        if min(short_burn, long_burn) > BURN_LIMIT:
            return f"SLO gate: control {metric} burn {short_burn:.2f}x / {long_burn:.2f}x (short / long)"
    return None


def shared_rates(case: str, tick: int) -> tuple[float, float]:
    if case == "sustained-shared" or (case == "late-shared" and tick >= fixed.WINDOWS - 1):
        return fixed.control_rates("shared-degradation", 0)
    if case == "recovered-shared" and tick < LONG:
        return 0.11, 0.20
    return fixed.control_rates("healthy", 0)


def paired_timeline(case: str, seed: int, *, scripted: bool = False):
    if case not in SCENARIOS:
        raise ValueError(f"Unknown SLO scenario: {case}")
    scenario = "early-fault" if case == "canary-only" else "healthy"
    rng = default_rng(seed)

    @cache
    def source(share, _block, tick):
        canary = fixed.REQUESTS * share // 100
        load = 0.0 if scripted else float(rng.choice((0.0, 0.0, 0.01, 0.025)))
        error, slow = (rate + load for rate in shared_rates(case, tick))
        extra_error, extra_slow = fixed.harm(scenario, share)
        return fixed.Evidence(
            fixed.sample_arm(rng, fixed.REQUESTS - canary, 0, error, slow, scripted=scripted),
            fixed.sample_arm(rng, canary, 0, error + extra_error, slow + extra_slow, scripted=scripted),
        )

    return (
        tuple(fixed.timeline(scenario, seed, window_source=source)),
        tuple(fixed.timeline(scenario, seed, window_source=source, health_gate=health_hold)),
    )


def run(trials: int):
    print(f"Paired simulated rollouts: {trials:,} seeds per scenario starting at {FIRST_SEED:,}")
    print(f"Control-only burn: {SHORT}/{LONG} closed windows above {BURN_LIMIT}x; "
          "rollback and checkpoint rules unchanged.")
    healthy_rates = fixed.control_rates("healthy", 0)
    for case in SCENARIOS:
        pairs = [paired_timeline(case, seed)
                 for seed in range(FIRST_SEED, FIRST_SEED + trials)]
        print(f"\n{case}")
        for policy, runs in zip(POLICIES, zip(*pairs), strict=True):
            endings = [events[-1] for events in runs]
            counts = Counter(event.action for event in endings)
            gate_holds = sum(event.reason.startswith("SLO gate:") for event in endings)
            print(f"  {policy}: promote {counts['PROMOTE']}; rollback {counts['ROLLBACK']}; "
                  f"hold {counts['HOLD']} (SLO {gate_holds}); "
                  f"mean terminal {mean(e.tick + 1 for e in endings):.2f} ticks; "
                  f"mean canary sends {mean(e.cumulative_canary for e in endings):.1f}")
            if case not in ("healthy", "canary-only"):
                shared_sends = mean(sum(e.sent_canary for e in events if shared_rates(case, e.tick) != healthy_rates)
                                    for events in runs)
                advanced = sum(any(e.action == "ADVANCE" for e in events) for events in runs)
                print(f"    mean canary sends during shared degradation {shared_sends:.1f}; "
                      f"advanced past 10% {advanced}")
        print(f"  promoted only without the SLO gate: "
              f"{sum(a[-1].action == 'PROMOTE' and b[-1].action != 'PROMOTE' for a, b in pairs)}; "
              f"mean canary sends avoided: "
              f"{mean(a[-1].cumulative_canary - b[-1].cumulative_canary for a, b in pairs):.1f}")


def show(case: str):
    print(f"\n{case} / scripted simulation (zero-based ticks)")
    for policy, events in zip(POLICIES, paired_timeline(case, 0, scripted=True), strict=True):
        for event in events:
            if event.terminal or event.action == "ADVANCE":
                print(f"  {policy:21} tick {event.tick:02d} | {event.share:2d}% | "
                      f"{event.action:8} | {event.cumulative_canary:,} canary sends | {event.reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trials", type=int, default=1000, help="paired seeds per scenario")
    mode.add_argument("--timeline", choices=("all", *SCENARIOS), help="show scripted decisions")
    args = parser.parse_args()
    if args.timeline:
        for case in SCENARIOS if args.timeline == "all" else (args.timeline,):
            show(case)
    elif args.trials < 1:
        parser.error("--trials must be positive")
    else:
        run(args.trials)


if __name__ == "__main__":
    main()
