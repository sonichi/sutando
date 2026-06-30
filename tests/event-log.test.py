#!/usr/bin/env python3
"""Tests for src/event_log.py.

event_log.py is a crash-safe structured logging module. Key invariants:
  - log_event() never raises, even on write failures.
  - Events are written as single-line JSON (JSONL).
  - Non-JSON-serializable values are repr()-stringified.
  - get_log_path() rolls to a new file at midnight local time.
  - Machine ID falls back to hostname when identity file is missing.
  - LOGS_DIR is created on demand.

Run: python3 tests/event-log.test.py
Exit 0 on pass, 1 on fail.
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
spec = importlib.util.spec_from_file_location("event_log", REPO / "src" / "event_log.py")
el = importlib.util.module_from_spec(spec)
spec.loader.exec_module(el)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_log_event_writes_jsonl() -> list[str]:
    """log_event() appends a valid JSONL line."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        saved = el.LOGS_DIR
        el.LOGS_DIR = Path(td) / "logs"
        try:
            el.log_event("test.kind", foo="bar", count=42)
            files = list(Path(td).glob("logs/events-*.jsonl"))
            check("no events file created", len(files) == 1, fails)
            if files:
                lines = files[0].read_text().splitlines()
                check("zero lines written", len(lines) == 1, fails)
                if lines:
                    evt = json.loads(lines[0])
                    check("kind mismatch", evt.get("kind") == "test.kind", fails)
                    check("foo missing", evt.get("foo") == "bar", fails)
                    check("count missing", evt.get("count") == 42, fails)
                    check("ts missing", "ts" in evt, fails)
                    check("node missing", "node" in evt, fails)
        finally:
            el.LOGS_DIR = saved
    return fails


def test_log_event_never_raises() -> list[str]:
    """log_event() must not raise on bad LOGS_DIR (read-only / missing parent)."""
    fails: list[str] = []
    saved = el.LOGS_DIR
    el.LOGS_DIR = Path("/nonexistent/path/that/cannot/be/created")
    try:
        el.log_event("test.crash_safe", should_not="crash")
        check("log_event raised unexpectedly", True, fails)
    except Exception as exc:
        fails.append(f"log_event raised: {exc}")
    finally:
        el.LOGS_DIR = saved
    return fails


def test_non_serializable_value_repr() -> list[str]:
    """Non-JSON-serializable values are repr()-stringified."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        saved = el.LOGS_DIR
        el.LOGS_DIR = Path(td) / "logs"
        try:
            unserializable = object()
            el.log_event("test.repr", bad=unserializable)
            files = list(Path(td).glob("logs/events-*.jsonl"))
            if files:
                evt = json.loads(files[0].read_text().splitlines()[0])
                val = evt.get("bad", "")
                # Should be repr(object()) — starts with "<object"
                check(
                    f"non-serializable not repr'd: {val!r}",
                    isinstance(val, str) and "object" in val,
                    fails,
                )
        finally:
            el.LOGS_DIR = saved
    return fails


def test_log_path_rolls_daily() -> list[str]:
    """get_log_path() returns a date-stamped file; different timestamps → different files."""
    fails: list[str] = []
    # Two timestamps that are guaranteed to be on different dates
    ts_a = 0.0        # 1970-01-01
    ts_b = 86400.0    # 1970-01-02
    path_a = el.get_log_path(ts_a)
    path_b = el.get_log_path(ts_b)
    check("paths should differ across dates", path_a != path_b, fails)
    check("path_a not .jsonl", path_a.suffix == ".jsonl", fails)
    check("path_b not .jsonl", path_b.suffix == ".jsonl", fails)
    # Both paths should be under LOGS_DIR
    check("path_a not under LOGS_DIR", str(path_a).startswith(str(el.LOGS_DIR)), fails)
    return fails


def test_log_path_same_for_same_day() -> list[str]:
    """Two log_event() calls in the same second go to the same file."""
    fails: list[str] = []
    now = time.time()
    path1 = el.get_log_path(now)
    path2 = el.get_log_path(now + 1)  # still the same date (same minute)
    # They should be the same file (same date) if within seconds of each other
    # — compare just the date part of the filename
    check(
        "same-day paths differ",
        path1.name[:17] == path2.name[:17],  # 'events-YYYY-MM-DD' prefix
        fails,
    )
    return fails


def test_machine_id_fallback() -> list[str]:
    """_machine_id() falls back to hostname when identity file is missing."""
    fails: list[str] = []
    # Reset cached value so the function runs fresh
    saved = el._CACHED_MACHINE
    el._CACHED_MACHINE = None
    import socket
    expected_host = socket.gethostname().split(".")[0]
    with tempfile.TemporaryDirectory() as td:
        # Patch personal_path to return a non-existent file
        try:
            from util_paths import personal_path as real_pp  # noqa: F401
        except ImportError:
            pass
        # Use a temp workspace where no stand-identity.json exists
        saved_ws = el.WORKSPACE_DIR
        el.WORKSPACE_DIR = Path(td)
        try:
            mid = el._machine_id()
            check(
                f"hostname fallback wrong: {mid!r} != {expected_host!r}",
                mid == expected_host or mid == "unknown",
                fails,
            )
        finally:
            el.WORKSPACE_DIR = saved_ws
            el._CACHED_MACHINE = saved
    return fails


def test_multiple_events_append() -> list[str]:
    """Multiple log_event() calls append to the same file."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        saved = el.LOGS_DIR
        el.LOGS_DIR = Path(td) / "logs"
        try:
            for i in range(5):
                el.log_event("test.append", seq=i)
            files = list(Path(td).glob("logs/events-*.jsonl"))
            check("no events file", len(files) == 1, fails)
            if files:
                lines = files[0].read_text().splitlines()
                check(f"expected 5 lines, got {len(lines)}", len(lines) == 5, fails)
                seqs = [json.loads(l).get("seq") for l in lines]
                check("seqs not 0-4", seqs == list(range(5)), fails)
        finally:
            el.LOGS_DIR = saved
    return fails


def test_event_has_required_fields() -> list[str]:
    """Every event must include ts (float), node (str), kind (str)."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        saved = el.LOGS_DIR
        el.LOGS_DIR = Path(td) / "logs"
        try:
            el.log_event("test.fields", x=1)
            files = list(Path(td).glob("logs/events-*.jsonl"))
            if files:
                evt = json.loads(files[0].read_text().splitlines()[0])
                check("ts not float", isinstance(evt.get("ts"), float), fails)
                check("node not str", isinstance(evt.get("node"), str), fails)
                check("kind not str", isinstance(evt.get("kind"), str), fails)
                check("ts <= 0", evt.get("ts", 0) > 0, fails)
        finally:
            el.LOGS_DIR = saved
    return fails


def test_logs_dir_created_on_demand() -> list[str]:
    """LOGS_DIR is created automatically by log_event()."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        logs_dir = Path(td) / "deep" / "nested" / "logs"
        saved = el.LOGS_DIR
        el.LOGS_DIR = logs_dir
        try:
            check("logs_dir pre-exists", not logs_dir.exists(), fails)
            el.log_event("test.mkdir")
            check("logs_dir not created", logs_dir.exists(), fails)
        finally:
            el.LOGS_DIR = saved
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("writes valid JSONL", test_log_event_writes_jsonl),
        ("never raises on write failure", test_log_event_never_raises),
        ("non-serializable value repr'd", test_non_serializable_value_repr),
        ("log path rolls daily", test_log_path_rolls_daily),
        ("same-day paths equal", test_log_path_same_for_same_day),
        ("machine ID hostname fallback", test_machine_id_fallback),
        ("multiple events append", test_multiple_events_append),
        ("required fields present", test_event_has_required_fields),
        ("LOGS_DIR created on demand", test_logs_dir_created_on_demand),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nevent-log: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
