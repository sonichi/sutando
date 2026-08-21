#!/usr/bin/env python3
"""A future-dated `.alive` must not read as a live core.

Five call sites asked "is this heartbeat fresh?" with a one-sided threshold
(`age >= max` / `age < max`). A future-dated file has a NEGATIVE age, so every
one of them accepted it as fresh — a clock step or a bad write reads as a live
core indefinitely, and in the socket resolvers it also outranks genuinely live
peers, since selection is max(mtime).

All five sites are driven through their real functions — _local_core_socket,
_fresh_local_core_record, _any_core_alive, _core_started_within and
_live_core_socket — not through the helper, so a site that stops calling the
helper fails here rather than passing on the helper's own behaviour. Each is
asserted in BOTH directions: a fix that rejected every heartbeat would satisfy
the future-dated half alone.

Run: python3 tests/heartbeat-freshness-bounds.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:                      # the module runs checks when executed directly
    pass

FUTURE_S = 3600.0
STALE_S = 600.0
FRESH_S = 10.0


def _write_alive(cores: Path, label: str, age_s: float, socket: str = "/tmp/s.sock"):
    """age_s < 0 means future-dated."""
    import time
    p = cores / f"{label}.alive"
    p.write_text(json.dumps({"host": label, "socket": socket, "started_at": 1.0}))
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def _label():
    labels = sorted(hc._local_host_labels())
    assert labels, "no local host label resolved"
    return labels[0]


def test_helper_rejects_both_ends():
    now = 1_000_000.0
    assert hc.heartbeat_is_fresh(now - FRESH_S, now) is True
    assert hc.heartbeat_is_fresh(now - STALE_S, now) is False, "stale must be rejected"
    assert hc.heartbeat_is_fresh(now + FUTURE_S, now) is False, "future-dated must be rejected"
    assert hc.heartbeat_is_fresh(now, now) is True, "age 0 is fresh"


def test_helper_tolerates_a_just_rewritten_heartbeat():
    # Callers snapshot `now` before they stat; an atomic rewrite in between makes
    # the mtime slightly NEWER than `now`. That interleave is normal, not corrupt.
    now = 1_000_000.0
    assert hc.heartbeat_is_fresh(now + 0.001, now) is True, (
        "sub-ms writer/reader interleave must not read as a dead core")
    assert hc.heartbeat_is_fresh(now + 1.0, now) is True, "small sync/clock skew tolerated"
    edge = hc.CORE_HEARTBEAT_FUTURE_TOLERANCE_S
    assert hc.heartbeat_is_fresh(now + edge + 0.001, now) is False, (
        "beyond the tolerance is still rejected — the bound is bounded")


def test_any_core_alive_survives_a_concurrent_heartbeat_rewrite():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, "rewritten", age_s=-0.5)
        assert hc._any_core_alive(workspace=ws) is True, (
            "a heartbeat 0.5s ahead of the reader snapshot is a live core mid-rewrite")


def test_live_core_socket_rejects_a_future_dated_heartbeat():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), -FUTURE_S, socket="/tmp/FUTURE.sock")
        got = hc._live_core_socket(workspace=ws)
        assert got != "/tmp/FUTURE.sock", f"future-dated heartbeat accepted as live: {got}"


def test_any_core_alive_rejects_a_future_dated_heartbeat():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), -FUTURE_S)
        assert hc._any_core_alive(workspace=ws) is False, "future-dated read as a live core"


def test_a_genuinely_fresh_heartbeat_is_still_accepted():
    """The bound must not cost the positive case - a fix that rejects everything
    would pass every assertion above."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), FRESH_S, socket="/tmp/LIVE.sock")
        assert hc._any_core_alive(workspace=ws) is True, "fresh heartbeat rejected"
        assert hc._live_core_socket(workspace=ws) == "/tmp/LIVE.sock"


def test_a_stale_heartbeat_is_still_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), STALE_S, socket="/tmp/STALE.sock")
        assert hc._any_core_alive(workspace=ws) is False
        assert hc._live_core_socket(workspace=ws) != "/tmp/STALE.sock"


def test_local_core_socket_rejects_a_future_dated_heartbeat():
    """This host's own socket resolver — a separate site from _live_core_socket."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), -FUTURE_S, socket="/tmp/FUTURE.sock")
        assert hc._local_core_socket(workspace=ws) != "/tmp/FUTURE.sock"


def test_local_core_socket_still_accepts_a_fresh_heartbeat():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), FRESH_S, socket="/tmp/LIVE.sock")
        assert hc._local_core_socket(workspace=ws) == "/tmp/LIVE.sock"


def test_fresh_local_core_record_rejects_a_future_dated_heartbeat():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), -FUTURE_S)
        assert hc._fresh_local_core_record(workspace=ws) is None


def test_fresh_local_core_record_still_returns_a_fresh_one():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        _write_alive(cores, _label(), FRESH_S)
        rec = hc._fresh_local_core_record(workspace=ws)
        assert isinstance(rec, dict) and rec.get("socket") == "/tmp/s.sock", rec


def test_core_started_within_rejects_a_future_dated_heartbeat():
    """started_at is recent, so only the heartbeat's own freshness can reject it."""
    with tempfile.TemporaryDirectory() as tmp:
        import time
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        p = _write_alive(cores, _label(), -FUTURE_S)
        p.write_text(json.dumps({"host": _label(), "socket": "/tmp/s.sock",
                                 "started_at": time.time()}))
        t = time.time() + FUTURE_S
        os.utime(p, (t, t))
        assert hc._core_started_within(3600.0, workspace=ws) is False


def test_core_started_within_still_sees_a_fresh_recent_core():
    with tempfile.TemporaryDirectory() as tmp:
        import time
        ws = Path(tmp)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        p = _write_alive(cores, _label(), FRESH_S)
        p.write_text(json.dumps({"host": _label(), "socket": "/tmp/s.sock",
                                 "started_at": time.time() - 5}))
        t = time.time() - FRESH_S
        os.utime(p, (t, t))
        assert hc._core_started_within(3600.0, workspace=ws) is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("PASS - heartbeat freshness is bounded on both ends")
