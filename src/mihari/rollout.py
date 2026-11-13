"""Fixed-share rollouts, delayed observations, and rollback rules."""

from dataclasses import dataclass, replace
from math import log
from random import Random


SCENARIOS = ("healthy", "shared-outage", "delayed-regression")
LOOKS = 12
ONSET = 4
GRACE = 2
MIN_CANARY = 4
SLOW_MS = 100
RULES = ("threshold", "sequential")


@dataclass(frozen=True, slots=True)
class Request:
    block: int
    canary: bool
    pair: int | None
    failed: bool
    latency_ms: int
    available_at: int | None


@dataclass(frozen=True, slots=True)
class Look:
    block: int
    at: int
    control: int
    canary: int
    control_failures: int
    canary_failures: int
    control_slow: int
    canary_slow: int
    canary_only: int
    control_only: int
    sent_canary: int
    exposed: int


@dataclass(frozen=True, slots=True)
class Decision:
    look: Look
    threshold_score: float | None
    sequential_score: float | None

    def score(self, rule: str) -> float | None:
        if rule == "threshold":
            return self.threshold_score
        if rule == "sequential":
            return self.sequential_score
        raise ValueError(f"Unknown rule: {rule}")

    def rollback(self, rule: str, cutoff: float) -> bool:
        score = self.score(rule)
        return score is not None and score > cutoff

    def reason(self, rule: str, cutoff: float) -> str:
        if self.look.canary < MIN_CANARY or self.look.control < MIN_CANARY:
            return "too few observed requests"
        score = self.score(rule)
        if score is None:
            return "waiting for a second eligible window"
        label = "two-window failure gap" if rule == "threshold" else "paired log likelihood ratio"
        return f"{label} {score:.3f} {'>' if score > cutoff else '<='} {cutoff:.3f}"


def make_trace(scenario: str, seed: int, *, scripted: bool = False, increase: float = 0.19,
               duration: int | None = None) -> tuple[tuple[Request, ...], ...]:
    """Assign one in ten requests to the canary before generating its outcome."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}")
    rng = Random(seed)
    blocks = []
    for block in range(LOOKS):
        groups = 4 if scripted else rng.randint(14, 26)
        shared = scenario == "shared-outage" and ONSET <= block < ONSET + 3
        regressed = (scenario == "delayed-regression" and block >= ONSET
                     and (duration is None or block < ONSET + duration))
        # One shared rate per block creates bursts and within-block dependence.
        baseline = 0.01 + 0.015 * (block // 4) + rng.choice((0, 0, 0, 0.06))
        if shared:
            baseline += 0.22
        rows = []
        for pair in range(groups):
            for slot in range(10):
                canary = slot == 0
                if scripted:
                    failed = (shared and pair == 0) or (regressed and canary)
                else:
                    failed = rng.random() < baseline + (increase if regressed and canary else 0)
                slow = failed or (not scripted and rng.random() < 0.04)
                delay = GRACE if regressed and canary else 0 if scripted else rng.randrange(GRACE + 1)
                rows.append(Request(block, canary, pair if slot < 2 else None, failed,
                                    160 if slow else 25, block + delay))
        blocks.append(tuple(rows))
    return tuple(blocks)


def observe(rows, block: int, at: int, sent_canary: int, exposed: int) -> Look:
    """Only outcomes received at this look can enter its counts or matched pairs."""
    visible = [row for row in rows if row.available_at is not None and row.available_at <= at]
    canary = [row for row in visible if row.canary]
    control = [row for row in visible if not row.canary]
    matched_control = {row.pair: row.failed for row in control if row.pair is not None}
    discordances = [(int(row.failed) - int(matched_control[row.pair])) for row in canary
                    if row.pair in matched_control]
    return Look(block, at, len(control), len(canary), sum(row.failed for row in control),
                sum(row.failed for row in canary), sum(row.latency_ms > SLOW_MS for row in control),
                sum(row.latency_ms > SLOW_MS for row in canary), discordances.count(1),
                discordances.count(-1), sent_canary, exposed)


class Monitor:
    """Compare a two-window threshold with Wald's test on discordant pairs."""

    def __init__(self):
        self.previous_gap: float | None = None
        self.llr = 0.0

    def update(self, look: Look) -> Decision:
        if min(look.canary, look.control) < MIN_CANARY:
            self.previous_gap = None
            return Decision(look, None, None)
        gap = look.canary_failures / look.canary - look.control_failures / look.control
        threshold = min(self.previous_gap, gap) if self.previous_gap is not None else None
        self.previous_gap = gap
        # Wald likelihood ratio for Bernoulli(0.75) versus Bernoulli(0.5).
        self.llr += look.canary_only * log(1.5) + look.control_only * log(0.5)
        return Decision(look, threshold, self.llr)


def replay(trace, onset: int | None = None, dispatch=None, *,
           complete_early: bool = False, fault_end: int | None = None):
    """Run the same monitor for simulated requests or actual loopback responses."""
    monitor = Monitor()
    sent_canary = exposed = 0
    received = []
    block = 0
    for at in range(len(trace) + GRACE):
        if at < len(trace):
            rows = trace[at]
            received.append(tuple(dispatch(row) for row in rows) if dispatch else rows)
            count = sum(row.canary for row in rows)
            sent_canary += count
            if onset is not None and at >= onset and (fault_end is None or at < fault_end):
                exposed += count
        while block < len(received):
            rows = received[block]
            if at < block + GRACE and (not complete_early or any(
                row.available_at is None or row.available_at > at for row in rows
            )):
                break
            yield monitor.update(observe(rows, block, at, sent_canary, exposed))
            block += 1


def with_response(request: Request, status: int, latency_ms: int) -> Request:
    return replace(request, failed=status >= 500, latency_ms=latency_ms)
