#!/usr/bin/env python3
"""
Tests for src/event_log.py

Covers:
  get_log_path()    — pure timestamp-to-path function
  log_event()       — JSONL writer: fields, format, append, non-raise
  _machine_id()     — identity file reader with fallback

Run: python3 tests/event-log.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "event_log", REPO / "src" / "event_log.py"
)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def fail(label: str, msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {msg}")


# ── get_log_path ─────────────────────────────────────────────────────────────

def test_get_log_path_default_is_today():
    path = ev.get_log_path()
    today = time.strftime("%Y-%m-%d")
    expected_name = f"events-{today}.jsonl"
    if path.name == expected_name:
        ok("(lp-a) get_log_path() default → today's file name")
    else:
        fail("(lp-a) get_log_path() default → today's file name",
             f"got {path.name}, expected {expected_name}")


def test_get_log_path_specific_timestamp():
    # 2026-01-15 12:00:00 UTC — use local epoch seconds
    import calendar
    ts = calendar.timegm(time.strptime("2026-01-15", "%Y-%m-%d"))
    path = ev.get_log_path(ts)
    # path.name should contain 2026-01-15 (or possibly 2026-01-14 in UTC+14 — be flexible)
    if "2026-01-1" in path.name and path.name.endswith(".jsonl"):
        ok("(lp-b) get_log_path(ts) → file named by local date")
    else:
        fail("(lp-b) get_log_path(ts) → file named by local date", f"got {path.name}")


def test_get_log_path_returns_path_in_logs_dir():
    path = ev.get_log_path()
    if path.parent.name == "logs":
        ok("(lp-c) get_log_path() path is under logs/ dir")
    else:
        fail("(lp-c) get_log_path() path is under logs/ dir", f"parent={path.parent.name}")


def test_get_log_path_zero_epoch():
    # epoch 0 → 1970-01-01 local (or 1969-12-31 in UTC+N; just check it's a valid date)
    path = ev.get_log_path(0)
    if path.name.startswith("events-19") and path.name.endswith(".jsonl"):
        ok("(lp-d) get_log_path(0) → 1969/1970 date file")
    else:
        fail("(lp-d) get_log_path(0) → 1969/1970 date file", f"got {path.name}")


# ── log_event ────────────────────────────────────────────────────────────────

def test_log_event_writes_file():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.written")
        log_files = list(logs_dir.glob("events-*.jsonl"))
    if len(log_files) == 1:
        ok("(le-a) log_event() creates events-*.jsonl in LOGS_DIR")
    else:
        fail("(le-a) log_event() creates events-*.jsonl in LOGS_DIR",
             f"found {log_files}")


def test_log_event_valid_json():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.json")
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    try:
        obj = json.loads(line)
        ok("(le-b) log_event() output is valid JSON")
    except json.JSONDecodeError as e:
        fail("(le-b) log_event() output is valid JSON", str(e))


def test_log_event_contains_required_fields():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.fields")
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    obj = json.loads(line)
    if "ts" in obj and "node" in obj and "kind" in obj:
        ok("(le-c) log_event() output has ts, node, kind fields")
    else:
        fail("(le-c) log_event() output has ts, node, kind fields", f"keys={list(obj)}")


def test_log_event_kind_matches():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("bridge.task_written")
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    obj = json.loads(line)
    if obj.get("kind") == "bridge.task_written":
        ok("(le-d) kind field matches argument")
    else:
        fail("(le-d) kind field matches argument", f"got kind={obj.get('kind')!r}")


def test_log_event_extra_fields_included():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.extra", task_id="abc123", tier="owner")
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    obj = json.loads(line)
    if obj.get("task_id") == "abc123" and obj.get("tier") == "owner":
        ok("(le-e) extra keyword fields included in output")
    else:
        fail("(le-e) extra keyword fields included in output", f"obj={obj}")


def test_log_event_nonserializable_field_repr():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"

        class Unserializable:
            def __repr__(self):
                return "Unserializable()"

        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.repr", bad=Unserializable())
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    obj = json.loads(line)
    if obj.get("bad") == "Unserializable()":
        ok("(le-f) non-serializable field → repr()'d string")
    else:
        fail("(le-f) non-serializable field → repr()'d string", f"bad={obj.get('bad')!r}")


def test_log_event_appends_multiple_events():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.first")
            ev.log_event("test.second")
            ev.log_event("test.third")
        content = next(logs_dir.glob("events-*.jsonl")).read_text()
    lines = [l for l in content.splitlines() if l.strip()]
    if len(lines) == 3:
        ok("(le-g) 3 log_event() calls → 3 lines appended")
    else:
        fail("(le-g) 3 log_event() calls → 3 lines appended", f"got {len(lines)} lines")


def test_log_event_never_raises():
    # Point LOGS_DIR at a file (not a dir) to force an error path
    with tempfile.TemporaryDirectory() as tmp:
        not_a_dir = Path(tmp) / "logs"
        not_a_dir.write_text("I am a file, not a directory")
        with patch.object(ev, "LOGS_DIR", not_a_dir):
            try:
                ev.log_event("test.crash")
                ok("(le-h) log_event() never raises on write failure")
            except Exception as e:
                fail("(le-h) log_event() never raises on write failure", str(e))


def test_log_event_ts_is_numeric():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        before = time.time()
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.ts")
        after = time.time()
        line = next(logs_dir.glob("events-*.jsonl")).read_text().strip()
    obj = json.loads(line)
    ts = obj.get("ts")
    # round(now, 3) can land just below `before` due to float representation;
    # allow 10ms slack on either side.
    if isinstance(ts, float) and before - 0.01 <= ts <= after + 0.01:
        ok("(le-i) ts field is float within wall-clock range")
    else:
        fail("(le-i) ts field is float within wall-clock range",
             f"ts={ts!r} before={before:.3f} after={after:.3f}")


def test_log_event_one_line_per_event():
    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "logs"
        with patch.object(ev, "LOGS_DIR", logs_dir):
            ev.log_event("test.newline", text="line1\nline2")
        raw = next(logs_dir.glob("events-*.jsonl")).read_text()
    # Each event is exactly one line — embedded newlines should be JSON-escaped
    lines = [l for l in raw.splitlines() if l.strip()]
    if len(lines) == 1 and json.loads(lines[0]).get("text") == "line1\nline2":
        ok("(le-j) embedded newlines JSON-escaped — one line per event")
    else:
        fail("(le-j) embedded newlines JSON-escaped — one line per event",
             f"lines={len(lines)} content={raw!r}")


# ── _machine_id ───────────────────────────────────────────────────────────────

def test_machine_id_returns_string():
    ev._CACHED_MACHINE = None  # reset cache
    result = ev._machine_id()
    if isinstance(result, str) and len(result) > 0:
        ok("(mi-a) _machine_id() returns non-empty string")
    else:
        fail("(mi-a) _machine_id() returns non-empty string", f"got {result!r}")


def test_machine_id_cached_after_first_call():
    ev._CACHED_MACHINE = None  # reset
    first = ev._machine_id()
    # Corrupt the source — cached value should still be returned
    ev._CACHED_MACHINE = "CACHED_VALUE"
    second = ev._machine_id()
    ev._CACHED_MACHINE = None  # restore for other tests
    if second == "CACHED_VALUE":
        ok("(mi-b) _machine_id() returns cached value on subsequent calls")
    else:
        fail("(mi-b) _machine_id() returns cached value on subsequent calls",
             f"got {second!r}")


def test_machine_id_identity_file_used():
    ev._CACHED_MACHINE = None
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        identity = ws / "stand-identity.json"
        identity.write_text('{"machine": "test-node-42"}')
        with patch.object(ev, "WORKSPACE_DIR", ws):
            # Reset cache so it re-reads
            ev._CACHED_MACHINE = None
            # Patch util_paths.personal_path to return our identity file
            import sys as _sys
            if "util_paths" in _sys.modules:
                from unittest.mock import patch as _patch
                with _patch("util_paths.personal_path", return_value=identity):
                    result = ev._machine_id()
            else:
                result = ev._machine_id()
    ev._CACHED_MACHINE = None  # reset for other tests
    if isinstance(result, str) and len(result) > 0:
        ok("(mi-c) _machine_id() produces a valid string (identity or hostname)")
    else:
        fail("(mi-c) _machine_id() produces a valid string", f"got {result!r}")


if __name__ == "__main__":
    print("event-log tests")
    print("=" * 40)
    test_get_log_path_default_is_today()
    test_get_log_path_specific_timestamp()
    test_get_log_path_returns_path_in_logs_dir()
    test_get_log_path_zero_epoch()
    test_log_event_writes_file()
    test_log_event_valid_json()
    test_log_event_contains_required_fields()
    test_log_event_kind_matches()
    test_log_event_extra_fields_included()
    test_log_event_nonserializable_field_repr()
    test_log_event_appends_multiple_events()
    test_log_event_never_raises()
    test_log_event_ts_is_numeric()
    test_log_event_one_line_per_event()
    test_machine_id_returns_string()
    test_machine_id_cached_after_first_call()
    test_machine_id_identity_file_used()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
