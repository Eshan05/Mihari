"""Compare staged rollout risk with common-time and share-dependent faults."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from functools import cache
from statistics import mean

from . import fixed_steps as fixed
from .adaptive_steps import draw_window, middle_for, observe_arm
from .guardrail import NULLS
from .matched_risk import CONFIDENCE, MARGIN, PairedCounts, rate_difference, risks_match
from .study import frequency


SEEDS = range(970_000, 972_000)
MIDDLE_TICK = fixed.WINDOWS + fixed.GRACE
FAULTS = ("common-time", "share-triggered")
POLICIES = ("fixed", "adaptive")


def fault(case: str, share: int, stage_tick: int) -> tuple[float, float]:
    if case == "common-time":
        return fixed.harm("larger-step-fault", fixed.SHARES[1]) if stage_tick >= MIDDLE_TICK else (0.0, 0.0)
    if case == "share-triggered":
        return fixed.harm("larger-step-fault", share)
    if case not in NULLS:
        raise ValueError(f"Unknown staged case: {case}")
    return 0.0, 0.0


def paired_timeline(case: str, seed: int, *, scripted: bool = False):
    base = case if case in NULLS else "healthy"

    @cache
    def stage_harm(share, stage_tick):
        return fault(case, share, stage_tick)

    @cache
    def inputs(tick):
        return draw_window(seed, tick)

    def source(share, block, tick):
        n = fixed.REQUESTS * share // 100
        extra_error, extra_slow = stage_harm(share, tick - block)
        if scripted:
            def arm(sent, error, slow):
                return fixed.Arm(sent, sent, round(sent * error), round(sent * slow))

            error, slow = fixed.control_rates(base, 0.0)
            control = arm(fixed.REQUESTS - n, error, slow)
            canary = arm(n, error + extra_error, slow + extra_slow)
            if tick < MIDDLE_TICK:
                canary = replace(canary, errors=2 + block % 2)
        else:
            draws = inputs(tick)
            error, slow = fixed.control_rates(base, draws.load)
            control = observe_arm(draws, draws.routing[n:], 0, error, slow)
            canary = observe_arm(draws, draws.routing[:n], 0, error + extra_error, slow + extra_slow)
        return fixed.Evidence(control, canary)

    def choose_middle(evidence):
        return middle_for(fixed.upper_harm(evidence))

    return (
        tuple(fixed.timeline(base, seed, window_source=source, stage_harm=stage_harm)),
        tuple(fixed.timeline(base, seed, window_source=source, stage_harm=stage_harm, choose_middle=choose_middle)),
    )


@dataclass(frozen=True, slots=True)
class Outcome:
    action: str
    activation: tuple[int, int] | None
    delay: int | None
    exposed: int


def outcome(events) -> Outcome:
    first = next(((event.tick, event.share) for event in events if event.faulty_canary), None)
    last = events[-1]
    delay = last.tick - first[0] if first is not None and last.action == "ROLLBACK" else None
    return Outcome(last.action, first, delay, last.faulty_canary)


def report_null(pairs):
    counts = PairedCounts.count(tuple(run.action == "ROLLBACK" for run in pair) for pair in pairs)
    for name, runs, rolled in zip(POLICIES, zip(*pairs), (counts.errors_only, counts.joint_only), strict=True):
        actions = Counter(run.action for run in runs)
        print(f"  {name}: false rollback {frequency(rolled + counts.both, counts.total)}; "
              f"promote {actions['PROMOTE']}; terminal hold {actions['HOLD']}")
    print(f"  Paired: fixed only {counts.errors_only}, adaptive only {counts.joint_only}, "
          f"both {counts.both}, neither {counts.neither}")
    interval = counts.interval(1 - (1 - CONFIDENCE) / len(NULLS))
    low, high = map(rate_difference, interval)
    print(f"  Adaptive minus fixed: {rate_difference(counts.difference)} "
          f"(simultaneous {CONFIDENCE:.0%} interval {low} to {high})")
    return interval


def report_fault(pairs):
    for name, runs in zip(POLICIES, zip(*pairs), strict=True):
        activations = Counter(run.activation for run in runs if run.activation is not None)
        delays = [run.delay for run in runs if run.delay is not None]
        never = sum(run.activation is None for run in runs)
        early = sum(run.activation is None and run.action == "ROLLBACK" for run in runs)
        timing = ", ".join(f"tick {tick} at {share}%: {count}"
                           for (tick, share), count in sorted(activations.items())) or "none"
        delay = f"{mean(delays):.2f} ticks" if delays else "n/a"
        print(f"  {name}: first affected sends {timing}")
        print(f"    never activated {never}/{len(runs)} (pre-fault rollbacks {early}); "
              f"detected {len(delays)}; activated misses {len(runs) - never - len(delays)}; "
              f"mean detection delay {delay} (detections only)")
        print(f"    mean affected-window canary sends {mean(run.exposed for run in runs):.1f}")

    def exposure_delta(group):
        return f"{mean(a.exposed - f.exposed for f, a in group):+.1f}" if group else "n/a"

    both = [(f, a) for f, a in pairs if f.activation is not None and a.activation is not None]
    fixed_only = sum(f.activation is not None and a.activation is None for f, a in pairs)
    adaptive_only = sum(f.activation is None and a.activation is not None for f, a in pairs)
    neither = len(pairs) - len(both) - fixed_only - adaptive_only
    print(f"  Paired activation: fixed only {fixed_only}, adaptive only {adaptive_only}, "
          f"both {len(both)}, neither {neither}")
    print(f"  Paired affected-window send difference (adaptive minus fixed): "
          f"{exposure_delta(pairs)} / all {len(pairs)} pairs; "
          f"{exposure_delta(both)} / {len(both)} jointly activated pairs")


def run():
    print(f"Staged rollout risk; {len(SEEDS):,} paired seeds per case ({SEEDS[0]} through {SEEDS[-1]})")
    print("Fixed middle 25%; adaptive middle 15%, 25%, or 35%; no checkpoint or threshold changes.")
    intervals = {}
    for case in NULLS:
        print(f"\n{case} / no regression")
        pairs = [tuple(map(outcome, paired_timeline(case, seed))) for seed in SEEDS]
        intervals[case] = report_null(pairs)
    print(f"\nRisk margin: +/-{100 * MARGIN:g} percentage points in both modeled nulls.")
    if risks_match(intervals):
        print("Verdict: false-rollback risks matched within the margin in both modeled nulls.")
    else:
        print("Verdict: matched risk not established; fault results show tradeoffs, not comparable-risk rankings.")
    print("This does not establish a 5% risk cap or production safety.")
    for case in FAULTS:
        print(f"\n{case} / canary latency")
        pairs = [tuple(map(outcome, paired_timeline(case, seed))) for seed in SEEDS]
        report_fault(pairs)


def show():
    print("Scripted comparison: canary sent, affected total, closed stage slow/seen (control vs canary), action.")
    width = 71
    for case in FAULTS:
        paths = paired_timeline(case, 0, scripted=True)
        print(f"\n{case} / scripted 15% adaptive middle")
        print(f" tick | {'fixed':{width}} | adaptive")
        for tick in range(max(map(len, paths))):
            cells = []
            for events in paths:
                if tick >= len(events):
                    cells.append("stopped")
                    continue
                event = events[tick]
                c, a = event.evidence.control, event.evidence.canary
                terminal = " (terminal)" if event.terminal else ""
                cells.append(f"{event.share:2d}% sent {event.sent_canary:3d}; affected {event.faulty_canary:4d}; "
                             f"{c.slow}/{c.seen} vs {a.slow}/{a.seen}; {event.action}{terminal}")
            print(f" {tick:02d}   | {cells[0]:{width}} | {cells[1]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", action="store_true", help="show scripted side-by-side timelines")
    if parser.parse_args().timeline:
        show()
    else:
        run()


if __name__ == "__main__":
    main()
