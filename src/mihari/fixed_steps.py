"""Simulate fixed canary steps with separate rollback and promotion decisions."""

import argparse
from collections import Counter
from dataclasses import dataclass
from statistics import mean

from numpy.random import Generator, default_rng
from scipy.stats import beta

from .guardrail import Guardrail
from .rollout import GRACE, Look
from .study import frequency


SHARES = (10, 25, 50)
WINDOWS = 8
REQUESTS = 1_000
ERROR_MARGIN = 0.03
SLOW_MARGIN = 0.05
CONTROL_ERROR_CAP = 0.08
CONTROL_SLOW_CAP = 0.15
ARM_ALPHA = 0.05 / (len(SHARES) * 2 * 2)
ROLLBACK_CUTOFF = 0.15
SCENARIOS = ("healthy", "early-fault", "larger-step-fault", "shared-degradation", "missing-telemetry", "near-margin")


@dataclass(frozen=True, slots=True)
class Arm:
    sent: int = 0
    seen: int = 0
    errors: int = 0
    slow: int = 0

    def __add__(self, other):
        return Arm(self.sent + other.sent, self.seen + other.seen,
                   self.errors + other.errors, self.slow + other.slow)


@dataclass(frozen=True, slots=True)
class Evidence:
    control: Arm = Arm()
    canary: Arm = Arm()

    def __add__(self, other):
        return Evidence(self.control + other.control, self.canary + other.canary)


@dataclass(frozen=True, slots=True)
class Event:
    tick: int
    share: int
    sent_control: int
    sent_canary: int
    evidence: Evidence
    action: str
    reason: str
    terminal: bool
    unsafe: bool
    cumulative_canary: int
    faulty_canary: int


def harm(scenario: str, share: int) -> tuple[float, float]:
    if scenario == "early-fault":
        return 0.20, 0.0
    if scenario == "larger-step-fault" and share >= 25:
        return 0.0, 0.22
    if scenario == "near-margin":
        return 0.04, 0.0
    return 0.0, 0.0


def sample_arm(rng: Generator, sent: int, late: int, errors: float, slow: float, *, scripted: bool) -> Arm:
    seen = sent - late
    if scripted:
        return Arm(sent, seen, round(seen * errors), round(seen * slow))
    # Mix shared and independent draws for the error/slow overlap.
    both = 0.50 * min(errors, slow) + 0.50 * errors * slow
    overlap, only_error, only_slow, _ = rng.multinomial(seen, (both, errors - both, slow - both,
                                                              1 - errors - slow + both))
    return Arm(sent, seen, int(overlap + only_error), int(overlap + only_slow))


def sample_window(scenario: str, share: int, block: int, rng: Generator, *, scripted: bool) -> Evidence:
    canary = REQUESTS * share // 100
    load = 0.0 if scripted else float(rng.choice((0.0, 0.0, 0.01, 0.025)))
    control_error, control_slow = (0.13, 0.25) if scenario == "shared-degradation" else (0.02, 0.05)
    extra_error, extra_slow = harm(scenario, share)
    late = int(scenario == "missing-telemetry" and share == SHARES[0] and block == WINDOWS - 1)
    return Evidence(
        sample_arm(rng, REQUESTS - canary, 0, control_error + load, control_slow + load, scripted=scripted),
        sample_arm(rng, canary, late, control_error + load + extra_error, control_slow + load + extra_slow,
                   scripted=scripted),
    )


def upper_difference(canary_hits: int, canary_n: int, control_hits: int, control_n: int) -> float:
    """Upper difference from two one-sided Clopper-Pearson arm bounds."""
    upper = 1.0 if canary_hits == canary_n else beta.isf(ARM_ALPHA, canary_hits + 1, canary_n - canary_hits)
    lower = 0.0 if control_hits == 0 else beta.ppf(ARM_ALPHA, control_hits, control_n - control_hits + 1)
    return float(upper - lower)


def checkpoint(evidence: Evidence, *, final: bool) -> tuple[str, str]:
    control, canary = evidence.control, evidence.canary
    if min(control.seen, canary.seen) == 0 or control.seen != control.sent or canary.seen != canary.sent:
        return "HOLD", "incomplete on-time telemetry at checkpoint"
    if control.errors / control.seen > CONTROL_ERROR_CAP or control.slow / control.seen > CONTROL_SLOW_CAP:
        return "HOLD", "control is degraded at checkpoint"
    error = upper_difference(canary.errors, canary.seen, control.errors, control.seen)
    slow = upper_difference(canary.slow, canary.seen, control.slow, control.seen)
    bounds = f"upper harm: errors {100 * error:+.2f} pp, slow {100 * slow:+.2f} pp"
    if error >= ERROR_MARGIN or slow >= SLOW_MARGIN:
        return "HOLD", f"harm limits not established ({bounds})"
    return ("PROMOTE" if final else "ADVANCE"), f"both harm bounds pass ({bounds})"


def timeline(scenario: str, seed: int, *, scripted: bool = False):
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown rollout scenario: {scenario}")
    rng = default_rng(seed)
    tick = cumulative_canary = faulty_canary = 0
    for share in SHARES:
        pending = []
        stage = Evidence()
        guardrail = Guardrail()
        errors, slow = harm(scenario, share)
        harmful = errors >= ERROR_MARGIN or slow >= SLOW_MARGIN
        for offset in range(WINDOWS + GRACE):
            draining = offset >= WINDOWS
            if draining:
                sent_control, sent_canary = REQUESTS, 0
            else:
                window = sample_window(scenario, share, offset, rng, scripted=scripted)
                pending.append(window)
                sent_control, sent_canary = window.control.sent, window.canary.sent
            cumulative_canary += sent_canary
            if errors > 0 or slow > 0:
                faulty_canary += sent_canary

            action, reason, terminal = "HOLD", "waiting for on-time evidence", False
            if draining:
                reason = "draining telemetry; only control receives new traffic"
            elif offset >= GRACE:
                reason = "waiting for fixed checkpoint"
            trigger = None
            if offset >= GRACE:
                # A window contributes only once, when its evidence deadline arrives.
                observed = pending[offset - GRACE]
                stage += observed
                c, v = observed.control, observed.canary
                look = Look(offset - GRACE, tick, c.seen, v.seen, c.errors, v.errors, c.slow,
                            v.slow, 0, 0, cumulative_canary, faulty_canary)
                trigger = guardrail.update(look).trigger("joint", ROLLBACK_CUTOFF)
            if trigger is not None:
                action, reason, terminal = "ROLLBACK", f"joint guardrail: {trigger}", True
            elif offset == WINDOWS + GRACE - 1:
                action, reason = checkpoint(stage, final=share == SHARES[-1])
                terminal = action != "ADVANCE"

            yield Event(tick, share, sent_control, sent_canary, stage, action, reason, terminal,
                        harmful and action in ("ADVANCE", "PROMOTE"), cumulative_canary, faulty_canary)
            if terminal:
                return
            tick += 1


def show(scenario: str):
    print(f"\n{scenario} / simulated fixed-step timeline")
    for event in timeline(scenario, 0, scripted=True):
        c, v = event.evidence.control, event.evidence.canary
        outcome = f"{event.action} (terminal)" if event.terminal else event.action
        print(f"tick {event.tick:02d} | stage {event.share:2d}% | "
              f"sends control/canary {event.sent_control}/{event.sent_canary} | "
              f"closed seen/sent control {c.seen}/{c.sent}, canary {v.seen}/{v.sent} | "
              f"stage errors {c.errors}/{v.errors}, slow {c.slow}/{v.slow} | "
              f"canary exposure {event.cumulative_canary}, faulty {event.faulty_canary} | "
              f"{outcome}: {event.reason}")


def run(trials: int):
    print(f"Simulated fixed steps: {' / '.join(f'{share}%' for share in SHARES)}; "
          f"{trials} seeded rollouts per scenario")
    print(f"{WINDOWS} windows of {REQUESTS:,} requests per stage; "
          f"{GRACE}-tick final drain; one advancement checkpoint per stage")
    print("Promotion confidence allowance is not a false-rollback budget.")
    for scenario in SCENARIOS:
        endings = []
        advances = unsafe_advances = unsafe_promotions = 0
        for seed in range(30_000, 30_000 + trials):
            events = tuple(timeline(scenario, seed))
            terminal = events[-1]
            endings.append(terminal)
            advances += sum(event.action == "ADVANCE" for event in events)
            unsafe_advances += sum(event.unsafe and event.action == "ADVANCE" for event in events)
            unsafe_promotions += terminal.unsafe and terminal.action == "PROMOTE"
        outcomes = Counter(event.action for event in endings)
        print(f"\n{scenario}: promoted {outcomes['PROMOTE']}, rolled back {outcomes['ROLLBACK']}, "
              f"terminal holds {outcomes['HOLD']}")
        if scenario in ("healthy", "shared-degradation"):
            print(f"  false rollback: {frequency(outcomes['ROLLBACK'], trials)}")
        promotions = outcomes["PROMOTE"]
        advance_rate = f"{unsafe_advances}/{advances}" if advances else "0 (no advances)"
        promotion_rate = f"{unsafe_promotions}/{promotions}" if promotions else "0 (no promotions)"
        print(f"  unsafe advances {advance_rate}; unsafe promotions {promotion_rate}")
        print(f"  mean time to terminal outcome {mean(e.tick + 1 for e in endings):.2f} ticks; "
              f"mean canary sends {mean(e.cumulative_canary for e in endings):.1f}; "
              f"mean faulty-window canary sends {mean(e.faulty_canary for e in endings):.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trials", type=int, default=200, help="seeded rollouts per scenario for the study")
    mode.add_argument("--timeline", choices=("all", *SCENARIOS), help="show a scripted rollout")
    args = parser.parse_args()
    if args.timeline:
        for scenario in SCENARIOS if args.timeline == "all" else (args.timeline,):
            show(scenario)
    elif args.trials < 1:
        parser.error("--trials must be positive")
    else:
        run(args.trials)


if __name__ == "__main__":
    main()
