from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest

from mihari import trace_replay as t


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
MARKER = 1_705_000_000 * t.US


def row(name, relative, duration=100_000, **extra):
    start = MARKER + relative
    return dict(traceID=name, serviceName="frontendservice", operationName="frontend",
                parentSpanID=None, startTime=start, startTimeMillis=start // 1000,
                duration=duration, statusCode=None) | extra


def parquet(rows):
    schema = pa.schema([(col, pa.string() if i < 4 else pa.int64()) for i, col in enumerate(t.COLUMNS)]
                       + [("statusCode", pa.int64())])
    buf = BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), buf)
    return BytesIO(buf.getvalue())


def valid_rows():
    return [row("before-range", -t.SIDE_US - 1), row("before", -1),
            row("at-marker", 0), row("after-range", t.SIDE_US)]


def test_audit_filters_non_frontend_nonroot_and_excludes_end_boundary():
    rows = valid_rows() + [row("rpc", 0, parentSpanID="before"),
                           row("collector", 0, operationName="Export", statusCode=0),
                           row("downstream", 0, serviceName="checkoutservice", statusCode=500)]
    audited = t.read_frontend(parquet(rows[::-1]), MARKER)
    assert (audited.spans, audited.roots) == (7, 4)
    assert [r.trace_id for r in audited.requests] == ["before", "at-marker"]
    assert all(r.duration == 100_000 and not hasattr(r, "status") for r in audited.requests)


@pytest.mark.parametrize(("bad", "error"), [
    (row("before", 1), "nonunique"),
    (row("", 1), "identity"),
    (row("bad-time", 1, startTimeMillis=2), "microsecond"),
    (row("bad-duration", 1, duration=-1), "microsecond"),
])
def test_malformed_frontend_data_fails_closed(bad, error):
    with pytest.raises(ValueError, match=error):
        t.read_frontend(parquet(valid_rows() + [bad]), MARKER)


def test_coverage_requires_both_five_minute_edges():
    with pytest.raises(ValueError, match="five-minute"):
        t.read_frontend(parquet(valid_rows()[:-1]), MARKER)


def test_rejects_incomplete_parquet_schema():
    buf = BytesIO()
    pq.write_table(pa.table({"startTime": [MARKER]}), buf)
    with pytest.raises(ValueError, match="schema"):
        t.read_frontend(BytesIO(buf.getvalue()), MARKER)


def test_window_assignment_is_outcome_independent_and_stable():
    records = tuple(t.Request(f"request-{i}", MARKER, i * 1000) for i in range(104))
    baseline = t.assign(records, MARKER, "fixture")
    perturbed = t.assign((replace(r, duration=1) for r in reversed(records)), MARKER, "fixture")
    window = baseline[10]
    assert (len(window.control), len(window.canary)) == (94, 10)
    assert [r.trace_id for r in window.canary] == [r.trace_id for r in perturbed[10].canary]
    assert not set(window.canary) & set(window.control)
    assert all(not w.canary and not w.control for w in baseline[:10] + baseline[11:])


def test_observability_uses_completion_and_overlay_moves_completion():
    pair = (t.Request("at-threshold", MARKER, 100_000),)
    assert t.observed(pair, MARKER, MARKER + 99_999, False) == (0, 0)
    assert t.observed(pair, MARKER, MARKER + 100_000, False) == (1, 0)
    assert t.observed(pair, MARKER, MARKER + 249_999, True) == (0, 0)
    assert t.observed(pair, MARKER, MARKER + 250_000, True) == (1, 1)
    pre = (t.Request("pre-marker", MARKER - 1, 100_000),)
    assert t.observed(pre, MARKER, MARKER + 100_000, True) == (1, 0)


def window(index, ctl_slow, can_slow, n=20):
    start = MARKER + index * t.WINDOW_US
    control = tuple(t.Request(f"control-{index}-{i}", start, 200_000 if i < ctl_slow else 25_000)
                    for i in range(20))
    canary = tuple(t.Request(f"canary-{index}-{i}", start, 200_000 if i < can_slow else 25_000)
                   for i in range(n))
    return t.Window(start, control, canary)


def test_strict_cutoff_and_ineligible_window_resets_the_streak():
    windows = (window(0, 1, 4), window(1, 1, 4), window(2, 1, 5), window(3, 0, 3, n=3),
               window(4, 1, 5), window(5, 1, 5))
    looks = t.replay(windows, MARKER, overlay=False)
    assert [look.eligible for look in looks] == [True, True, True, False, True, True]
    assert [look.rollback for look in looks] == [False] * 5 + [True]


def test_exposure_counts_inflight_and_same_tick_sends_but_not_future():
    windows = (window(-1, 0, 0), window(0, 0, 0), window(1, 0, 0),
               window(2, 0, 0), window(3, 0, 0), window(4, 0, 0), window(5, 0, 0))
    aa = t.replay(windows, MARKER, overlay=False)
    overlay = t.replay(windows, MARKER, overlay=True)
    stop = overlay[-1]
    assert len(aa) == 7 and not aa[-1].rollback
    assert stop.rollback and stop.send_window == MARKER + t.WINDOW_US
    assert stop.deadline == MARKER + 4 * t.WINDOW_US
    assert stop.sent == 120 and stop.post_marker_sent == 100
    assert sum(look.canary_seen for look in overlay) == 60


def test_pre_marker_evidence_is_not_labeled_as_modeled_detection(capsys):
    looks = t.replay((window(-2, 0, 8), window(-1, 0, 8)), MARKER, overlay=True)
    assert t.summarize(looks, MARKER, overlay=True, show_timeline=True) == (True, False)
    assert "rollback at +60s, pre-marker evidence" in capsys.readouterr().out


def test_bounded_fetch_verifies_content_and_offline_replay(monkeypatch, tmp_path):
    case = t.CASES[0]
    good = b"abc"
    digest = sha256(good).hexdigest()
    requested = []

    def fetch(url, timeout):
        requested.append((url, timeout))
        return BytesIO(good)

    monkeypatch.setattr(t, "urlopen", fetch)
    with pytest.raises(ValueError, match="missing"):
        t.verified_file(tmp_path, case, "traces.parquet", len(good), digest, fetch=False)
    assert t.verified_file(tmp_path, case, "traces.parquet", len(good), digest, fetch=True) == good
    assert t.verified_file(tmp_path, case, "traces.parquet", len(good), digest, fetch=False) == good
    assert len(requested) == 1 and t.REVISION in requested[0][0]
    with pytest.raises(ValueError, match="pinned"):
        t.verified_file(tmp_path, case, "inject_time.txt", len(good), "wrong", fetch=True)
    assert not (tmp_path / case.name / "inject_time.txt").exists()
