"""Compare one planned look and repeated looks with sticky tenant assignment."""

import argparse

import numpy as np

from . import clustered_requests as clustered
from .study import frequency


LOOKS = 20
NO_STOP = LOOKS + 1
CANARY_PER_LOOK = clustered.CANARY_TENANTS * clustered.ATTEMPTS
FIRST_SEED = 1_260_000
UNITS = ("request", "tenant")
SCHEDULES = ("final only", "repeated 5%", "split 5% budget")


def p_value_paths(blocks: np.ndarray, canary: np.ndarray):
    requests = clustered.ATTEMPTS * np.arange(1, LOOKS + 1)
    return clustered.assignment_p_values(blocks.cumsum(axis=1), canary, requests)


def rollback_looks(path: np.ndarray) -> tuple[int, int, int]:
    def first_crossing(cutoff):
        hits = np.flatnonzero(path <= cutoff)
        return int(hits[0]) + 1 if hits.size else NO_STOP

    final = LOOKS if path[-1] <= clustered.ALPHA else NO_STOP
    return final, first_crossing(clustered.ALPHA), first_crossing(clustered.ALPHA / LOOKS)


def canary_sends(stop_looks):
    """Count sends through the decision, or through the deadline for misses."""
    return CANARY_PER_LOOK * np.minimum(stop_looks, LOOKS)


def run(trials: int):
    print(f"{trials:,} paired runs per case, seeds starting at {FIRST_SEED:,}.")
    print(f"{LOOKS} planned looks; {CANARY_PER_LOOK} canary sends per look. "
          f"Repeated thresholds: {clustered.ALPHA:.2%} raw or {clustered.ALPHA / LOOKS:.2%} split.")
    for bursts, fault in ((False, False), (True, False), (False, True), (True, True)):
        stops = np.empty((trials, len(UNITS), len(SCHEDULES)), dtype=np.intp)
        for i in range(trials):
            blocks, canary = clustered.draw_blocks(FIRST_SEED + i, bursts=bursts, fault=fault, blocks=LOOKS)
            stops[i] = [rollback_looks(path) for path in p_value_paths(blocks, canary)]
        traffic = "retry-like bursts" if bursts else "independent requests"
        workload = "canary regression" if fault else "no version effect"
        print(f"\n{traffic} / {workload}:")
        for unit_index, unit in enumerate(UNITS):
            for schedule_index, schedule in enumerate(SCHEDULES):
                looks = stops[:, unit_index, schedule_index]
                count = int(np.count_nonzero(looks != NO_STOP))
                metric = "detections" if fault else "false rollbacks"
                result = frequency(count, trials)
                if fault:
                    result += (f"; mean look {looks.mean():.2f} (miss={NO_STOP}); "
                               f"mean affected canary sends {canary_sends(looks).mean():.1f}")
                print(f"  {unit:7} {schedule:15} {metric}: {result}")


def show():
    blocks = np.zeros((clustered.TENANTS, LOOKS), dtype=np.intp)
    blocks[:, 4::5] = 1
    blocks[0, 1] = clustered.ATTEMPTS
    canary = np.arange(clustered.CANARY_TENANTS)
    print("A/A example: identical versions; one canary tenant has a burst at look 2.")
    for unit, path in zip(UNITS, p_value_paths(blocks, canary), strict=True):
        print(f"{unit} p at look 2: {path[1]:.5g}")
        for schedule, look in zip(SCHEDULES, rollback_looks(path), strict=True):
            action = f"rollback at look {look}" if look != NO_STOP else "no rollback"
            print(f"  {schedule:15} {action}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trials", type=int, default=4000, help="paired runs per case")
    mode.add_argument("--example", action="store_true", help="show a scripted burst on identical versions")
    args = parser.parse_args()
    if args.example:
        show()
    elif args.trials < 1:
        parser.error("--trials must be positive")
    else:
        run(args.trials)


if __name__ == "__main__":
    main()
