"""Replay recorded Online Boutique requests as a latency-only pseudo-canary."""

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from .fixed_steps import ROLLBACK_CUTOFF
from .rollout import GRACE, MIN_CANARY, SLOW_MS


REVISION = "8b6c06a7a15bcecdbf70b611a13b789c5925dc17"
SOURCE = f"https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/{REVISION}"
US = 1_000_000
WINDOW_US = 30 * US
SIDE_US = 5 * 60 * US
OVERLAY_US = 150_000
GAP_CUTOFF = Fraction(str(ROLLBACK_CUTOFF))
COLUMNS = ("traceID", "serviceName", "operationName", "parentSpanID",
           "startTime", "startTimeMillis", "duration")


@dataclass(frozen=True, slots=True)
class Case:
    suffix: str
    marker: int
    size: int
    digest: str

    @property
    def name(self):
        return f"re2ob_checkoutservice_{self.suffix}"


CASES = (
    Case("delay_1", 1705666511, 10233639, "a86818c7ab72aa360c3d81aafd9c4dc6a4da0ef75204cbf3a0c70d07a1fd44da"),
    Case("delay_2", 1705668921, 10149026, "7280e269cfc28daa5bc6e44b184ec430996295dd203f55fc2d8fabfff289c02c"),
    Case("delay_3", 1705671342, 10221446, "6e12fbdf92a66fab23bfe24342c3829038d847e751b8b5fd6dc224ef5d62c5e4"),
    Case("loss_1", 1705376266, 9984733, "4ac9a4ca2bc346ca9713131b2a6f38967437014bfe6070807d3b31364fdb80be"),
    Case("loss_2", 1705673762, 10225405, "aff63f170543593cc282d1aff27d3825aa6e98e830e649f04f35200b1672f9bf"),
    Case("loss_3", 1708489141, 10145276, "fb202a1b992eb5fe116da572081bd9616312d2ff5e250ccf359a5c63c71abed6"),
)


@dataclass(frozen=True, slots=True)
class Request:
    trace_id: str
    start: int
    duration: int


@dataclass(frozen=True, slots=True)
class AuditedTrace:
    spans: int
    roots: int
    requests: tuple[Request, ...]


def verified_file(directory: Path, case: Case, filename: str, size: int, digest: str, *, fetch: bool) -> bytes:
    path = directory / case.name / filename
    missing = not path.exists()
    if missing:
        if not fetch:
            raise ValueError(f"missing {filename}; rerun with --fetch")
        with urlopen(f"{SOURCE}/{case.name}/{filename}", timeout=30) as response:
            payload = response.read(size + 1)
    else:
        if path.stat().st_size != size:
            raise ValueError(f"unexpected {filename} size")
        payload = path.read_bytes()
    if len(payload) != size or sha256(payload).hexdigest() != digest:
        raise ValueError(f"{filename} did not match the pinned source")
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(payload)
    return payload


def read_frontend(file, marker: int) -> AuditedTrace:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(file)
    schema = parquet.schema_arrow
    if not all(name in schema.names for name in COLUMNS):
        raise ValueError("trace schema is missing required frontend columns")
    for name in COLUMNS[:4]:
        dtype = schema.field(name).type
        if not (pa.types.is_string(dtype) or pa.types.is_large_string(dtype)):
            raise ValueError(f"unexpected trace column type for {name}")
    for name in COLUMNS[4:]:
        if not pa.types.is_int64(schema.field(name).type):
            raise ValueError(f"unexpected trace clock type for {name}")

    table = parquet.read(columns=COLUMNS)
    roots = table.filter(pc.and_(
        pc.and_(pc.equal(table["serviceName"], "frontendservice"), pc.equal(table["operationName"], "frontend")),
        pc.equal(pc.fill_null(table["parentSpanID"], ""), ""),
    )).select(("traceID", "startTime", "startTimeMillis", "duration"))
    low, high = marker - SIDE_US, marker + SIDE_US
    coverage_start = coverage_end = None
    ids = set()
    requests = []
    for row in roots.to_pylist():
        trace_id, start, millis, duration = (row["traceID"], row["startTime"],
                                              row["startTimeMillis"], row["duration"])
        if not trace_id or trace_id in ids:
            raise ValueError("missing or nonunique frontend trace identity")
        if (start is None or millis is None or duration is None or start <= 0 or
                start // 1000 != millis or duration < 0):
            raise ValueError("invalid or inconsistent microsecond frontend timing")
        ids.add(trace_id)
        coverage_start = start if coverage_start is None else min(coverage_start, start)
        coverage_end = start if coverage_end is None else max(coverage_end, start)
        if low <= start < high:
            requests.append(Request(trace_id, start, duration))
    if (coverage_start is None or coverage_start > low or coverage_end < high or
            not any(r.start < marker for r in requests) or not any(r.start >= marker for r in requests)):
        raise ValueError("frontend input does not cover both five-minute sides of the marker")
    return AuditedTrace(len(table), len(roots), tuple(sorted(requests, key=lambda r: (r.start, r.trace_id))))


@dataclass(frozen=True, slots=True)
class Window:
    start: int
    control: tuple[Request, ...]
    canary: tuple[Request, ...]


def assign(requests, marker: int, case_name: str) -> tuple[Window, ...]:
    low = marker - SIDE_US
    groups = [[] for _ in range(2 * SIDE_US // WINDOW_US)]
    for request in requests:
        if low <= request.start < marker + SIDE_US:
            groups[(request.start - low) // WINDOW_US].append(request)
    windows = []
    for index, group in enumerate(groups):
        ranked = sorted(group, key=lambda r: (sha256(f"{case_name}:{r.trace_id}".encode()).digest(), r.trace_id))
        n = len(ranked) // 10
        windows.append(Window(low + index * WINDOW_US, tuple(ranked[n:]), tuple(ranked[:n])))
    return tuple(windows)


@dataclass(frozen=True, slots=True)
class Look:
    send_window: int
    deadline: int
    control_seen: int
    control_slow: int
    canary_seen: int
    canary_slow: int
    eligible: bool
    rollback: bool
    sent: int
    post_marker_sent: int


def observed(requests, marker: int, deadline: int, overlay: bool) -> tuple[int, int]:
    seen = slow = 0
    for event in requests:
        duration = event.duration + (OVERLAY_US if overlay and event.start >= marker else 0)
        if event.start + duration <= deadline:
            seen += 1
            slow += duration > SLOW_MS * 1000
    return seen, slow


def replay(windows, marker: int, *, overlay: bool) -> tuple[Look, ...]:
    starts = sorted(request.start for window in windows for request in window.canary)
    pre = bisect_left(starts, marker)
    consecutive = 0
    looks = []
    for window in windows:
        deadline = window.start + (1 + GRACE) * WINDOW_US
        control_n, control_slow = observed(window.control, marker, deadline, False)
        canary_n, canary_slow = observed(window.canary, marker, deadline, overlay)
        eligible = min(control_n, canary_n) >= MIN_CANARY
        difference = canary_slow * control_n - control_slow * canary_n
        crossing = (eligible and difference * GAP_CUTOFF.denominator
                    > GAP_CUTOFF.numerator * canary_n * control_n)
        consecutive = consecutive + 1 if crossing else 0
        sent = bisect_right(starts, deadline)
        looks.append(Look(window.start, deadline, control_n, control_slow, canary_n, canary_slow,
                          eligible, consecutive >= 2, sent, max(0, sent - pre)))
        if looks[-1].rollback:
            break
    return tuple(looks)


def summarize(looks, marker: int, *, overlay: bool, show_timeline: bool):
    last = looks[-1]
    label = "modeled +150 ms canary" if overlay else "A/A, recorded outcomes"
    if last.rollback:
        signal = "post-marker" if last.send_window >= marker else "pre-marker"
        verdict = f"rollback at {(last.deadline - marker) / US:+.0f}s, {signal} evidence"
    else:
        verdict = "no rollback"
    print(f"  {label}: {verdict}; ineligible windows {sum(not look.eligible for look in looks)}/{len(looks)}; "
          f"pseudo-canary sends {last.sent} (post-marker {last.post_marker_sent})")
    if show_timeline:
        for look in looks:
            action = "ROLLBACK" if look.rollback else "eligible" if look.eligible else "ineligible"
            print(f"    send window {(look.send_window - marker) / US:+4.0f}s, "
                  f"decision {(look.deadline - marker) / US:+4.0f}s | "
                  f"slow/seen control {look.control_slow}/{look.control_seen}, "
                  f"canary {look.canary_slow}/{look.canary_seen} | "
                  f"canary sent {look.sent} | {action}")
    return last.rollback, last.rollback and last.send_window >= marker


def run(directory: Path, *, fetch: bool, timeline: str | None):
    print(f"RCAEval {REVISION} | recorded testbed traffic, simulated assignment and delay")
    print("Latency only; HTTP status and ingestion times are not inferred. Completion gates visibility.")
    aa_triggers = detected = 0
    for case in CASES:
        try:
            marker_bytes = str(case.marker).encode()
            marker_digest = sha256(marker_bytes).hexdigest()
            verified_file(directory, case, "inject_time.txt", len(marker_bytes), marker_digest, fetch=fetch)
            trace = verified_file(directory, case, "traces.parquet", case.size, case.digest, fetch=fetch)
            audited = read_frontend(BytesIO(trace), case.marker * US)
        except (OSError, ValueError) as error:
            raise ValueError(f"{case.name}: {error}") from error
        marker = case.marker * US
        windows = assign(audited.requests, marker, case.name)
        print(f"\n{case.name} | marker {case.marker} | traces SHA-256 {case.digest}")
        print(f"  trace bytes {case.size}; marker bytes {len(marker_bytes)}; marker SHA-256 {marker_digest}")
        print(f"  spans {audited.spans}; root frontend {audited.roots}; selected {len(audited.requests)}; "
              f"excluded non-frontend/non-root {audited.spans - audited.roots}; "
              f"outside interval {audited.roots - len(audited.requests)}")
        aa, _ = summarize(replay(windows, marker, overlay=False), marker, overlay=False,
                          show_timeline=timeline == case.suffix)
        _, hit = summarize(replay(windows, marker, overlay=True), marker, overlay=True,
                           show_timeline=timeline == case.suffix)
        aa_triggers += aa
        detected += hit
    print(f"\nCases: {len(CASES)} | A/A triggers {aa_triggers}; "
          f"modeled delay detected {detected}; missed {len(CASES) - detected}")
    print("These are testbed replay observations, not real deployment outcomes or production risk estimates.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="local data folder outside the repository")
    parser.add_argument("--fetch", action="store_true", help="fetch missing pinned source files only")
    parser.add_argument("--timeline", choices=[case.suffix for case in CASES], help="also print this case's windows")
    args = parser.parse_args()
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        parser.error('Install the "trace" extra to replay Parquet files.')
    try:
        run(args.data_dir, fetch=args.fetch, timeline=args.timeline)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
