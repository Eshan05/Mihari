"""Compare fixed and bounded adaptive canary steps on paired workloads."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from functools import cache
from statistics import mean

from numpy import count_nonzero, where
from numpy.random import default_rng
from numpy.typing import NDArray

from . import fixed_steps as fixed
from .study import frequency


FIRST_SEED = 40_000
MIDDLES = (15, 25, 35)
EXAMPLES = ("strong-evidence", "barely-passing", "blocked")


@dataclass(frozen=True, slots=True)
class Draws:
    load: float
    routing: NDArray
    errors: NDArray
    slow: NDArray


def draw_window(seed: int, tick: int) -> Draws:
    rng = default_rng((seed, tick))
    load = float(rng.choice((0.0, 0.0, 0.01, 0.025)))
    routing = rng.permutation(fixed.REQUESTS)
    errors = rng.random(fixed.REQUESTS)
    slow = where(rng.random(fixed.REQUESTS) < 0.5, errors, rng.random(fixed.REQUESTS))
    return Draws(load, routing, errors, slow)


def observe_arm(draws: Draws, requests: NDArray, late: int, errors: float, slow: float) -> fixed.Arm:
    observed = requests[late:]
    return fixed.Arm(len(requests), len(observed), count_nonzero(draws.errors[observed] < errors),
                     count_nonzero(draws.slow[observed] < slow))


def sample(scenario: str, share: int, block: int, draws: Draws) -> fixed.Evidence:
    n = fixed.REQUESTS * share // 100
    error, slow = fixed.control_rates(scenario, draws.load)
    extra_error, extra_slow = fixed.harm(scenario, share)
    late = fixed.late_canary_count(scenario, share, block)
    return fixed.Evidence(
        observe_arm(draws, draws.routing[n:], 0, error, slow),
        observe_arm(draws, draws.routing[:n], late, error + extra_error, slow + extra_slow),
    )


def middle_for(bounds: tuple[float, float]) -> int:
    error, slow = bounds
    ratio = max(error / fixed.ERROR_MARGIN, slow / fixed.SLOW_MARGIN)
    if ratio <= 0.70:
        return MIDDLES[2]
    if ratio >= 0.90:
        return MIDDLES[0]
    return MIDDLES[1]


def paired_timeline(scenario: str, seed: int, *, source=None):
    if source is None:
        @cache
        def inputs(tick):
            return draw_window(seed, tick)

        def paired_source(share, block, tick):
            return sample(scenario, share, block, inputs(tick))

        source = paired_source
    baseline = tuple(fixed.timeline(scenario, seed, window_source=source))
    adaptive = tuple(fixed.timeline(scenario, seed, window_source=source,
                                    choose_middle=lambda evidence: middle_for(fixed.upper_harm(evidence))))
    return baseline, adaptive


def scripted_pair(example: str):
    scenario = "missing-telemetry" if example == "blocked" else "healthy"
    rng = default_rng(0)

    def source(share, block, _tick):
        window = fixed.sample_window(scenario, share, block, rng, scripted=True)
        if share != fixed.SHARES[0] or example == "blocked":
            return window
        errors = 1 if example == "strong-evidence" else 2 + block % 2
        return replace(window, canary=replace(window.canary, errors=errors))

    return paired_timeline(scenario, 0, source=source)


def show(example: str):
    baseline, adaptive = scripted_pair(example)
    print(f"\n{example} / scripted comparison")
    print("Both policies use the same first-stage evidence. Only the middle share may change.")
    print(" tick | fixed schedule                                    | adaptive schedule")
    for offset in range(max(len(baseline), len(adaptive))):
        cells = []
        for events in (baseline, adaptive):
            if offset >= len(events):
                cells.append("stopped")
            else:
                event = events[offset]
                decision = event.action
                if event.action == "ADVANCE":
                    decision += f" to {events[offset + 1].share}%"
                if event.terminal and event.action == "HOLD":
                    decision += " (terminal)"
                cells.append(f"{event.share:2d}% | {decision:18} | canary sent {event.sent_canary:3d}; "
                             f"total {event.cumulative_canary}")
        print(f" {offset:02d}   | {cells[0]:49} | {cells[1]}")
        for label, events in (("fixed", baseline), ("adaptive", adaptive)):
            if offset < len(events) and (events[offset].terminal or events[offset].action == "ADVANCE"):
                print(f"      {label}: {events[offset].reason}")


def reached(events, share):
    return any(event.share >= share and event.sent_canary for event in events)


def write_policy(name, runs, scenario):
    last = [events[-1] for events in runs]
    outcomes = Counter(event.action for event in last)
    stage_ticks = fixed.WINDOWS + fixed.GRACE
    middles = Counter(events[stage_ticks].share for events in runs if len(events) > stage_ticks)
    final_checkpoints = sum(event.share == fixed.SHARES[-1]
                            and event.tick == len(fixed.SHARES) * stage_ticks - 1
                            and event.action != "ROLLBACK" for event in last)
    print(f"  {name}: promoted {outcomes['PROMOTE']}, rolled back {outcomes['ROLLBACK']}, "
          f"terminal holds {outcomes['HOLD']}")
    if scenario in ("healthy", "shared-degradation"):
        print(f"    false rollback: {frequency(outcomes['ROLLBACK'], len(runs))}")
    print(f"    middle shares: {', '.join(f'{share}%: {middles[share]}' for share in MIDDLES)}; "
          f"no middle: {len(runs) - middles.total()}; reached 50%: {sum(reached(events, 50) for events in runs)}; "
          f"evaluated final checkpoint: {final_checkpoints}")
    if scenario == "larger-step-fault":
        first_fault = Counter(next(event.share for event in events if event.faulty_canary > 0)
                              for events in runs if events[-1].faulty_canary > 0)
        activated = first_fault.total()
        rolled = sum(event.faulty_canary > 0 and event.action == "ROLLBACK" for event in last)
        stopped_after_15 = sum(len(events) > stage_ticks and events[stage_ticks].share == 15
                               and events[-1].faulty_canary == 0 for events in runs)
        print(f"    reached fault share (>=25%): {activated}/{len(runs)}; "
              f"rolled back after activation: {rolled} of {activated}")
        print(f"    first activated at 25%: {first_fault[25]}, 35%: {first_fault[35]}, "
              f"50%: {first_fault[50]}; stopped after 15% without activation: {stopped_after_15}")
    advances = [event for events in runs for event in events if event.action == "ADVANCE"]
    unsafe_promotes = sum(event.unsafe and event.action == "PROMOTE" for event in last)
    print(f"    unsafe advances {sum(event.unsafe for event in advances)} of {len(advances)}; "
          f"unsafe promotions {unsafe_promotes} of {outcomes['PROMOTE']}")
    print(f"    mean terminal time {mean(event.tick + 1 for event in last):.2f} ticks; "
          f"mean canary sends {mean(event.cumulative_canary for event in last):.1f}; "
          f"mean faulty-window canary sends {mean(event.faulty_canary for event in last):.1f}")


def run(trials: int):
    print(f"Fixed vs adaptive middle share; {trials} paired seeds per scenario, starting at {FIRST_SEED:,}")
    print("Same evidence rules and request-level inputs; changing share can activate a fault.")
    print("Paired differences are descriptive, not equal-risk or production claims.")
    for scenario in fixed.SCENARIOS:
        pairs = [paired_timeline(scenario, seed) for seed in range(FIRST_SEED, FIRST_SEED + trials)]
        print(f"\n{scenario}")
        baseline, adaptive = zip(*pairs, strict=True)
        write_policy("fixed", baseline, scenario)
        write_policy("adaptive", adaptive, scenario)
        ends = [(a[-1], b[-1]) for a, b in pairs]
        print(f"  paired mean difference (adaptive minus fixed): "
              f"time {mean(b.tick - a.tick for a, b in ends):+.2f} ticks; "
              f"canary sends {mean(b.cumulative_canary - a.cumulative_canary for a, b in ends):+.1f}; "
              f"faulty-window sends {mean(b.faulty_canary - a.faulty_canary for a, b in ends):+.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trials", type=int, default=200, help="paired rollouts per scenario")
    mode.add_argument("--timeline", choices=("all", *EXAMPLES), help="show the two policies side by side")
    args = parser.parse_args()
    if args.timeline:
        for example in EXAMPLES if args.timeline == "all" else (args.timeline,):
            show(example)
    elif args.trials < 1:
        parser.error("--trials must be positive")
    else:
        run(args.trials)


if __name__ == "__main__":
    main()
