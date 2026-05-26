#!/usr/bin/env python3
"""Behavioral tests for `src/event_log.py`.

`event_log` provides crash-safe structured event logging for every Sutando
service. These tests guard the file-writing contract, JSONL format,
field inclusion, non-serializable-value handling, and the atomicity-safe
append behavior.

Run: python3 tests/event-log.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader — fresh import with a controlled temp workspace so
# log files don't land in the real workspace.
# -----------------------------------------------------------------------
_WORKSPACE = tempfile.mkdtemp(prefix="sutando-test-evlog-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("event_log", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("event_log", _SRC / "event_log.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log_event = _mod.log_event
get_log_path = _mod.get_log_path

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _last_event(path: Path) -> dict:
    """Read the last JSONL line from `path` and parse it."""
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    return json.loads(line)


def _events(path: Path) -> list[dict]:
    """Read all JSONL events from `path`."""
    return [json.loads(l) for l in path.read_text(encoding="utf-8").strip().splitlines() if l]


# -----------------------------------------------------------------------
# T1 — get_log_path returns a Path ending in events-YYYY-MM-DD.jsonl
# -----------------------------------------------------------------------
import re as _re
_path = get_log_path()
if isinstance(_path, Path) and _re.fullmatch(r"events-\d{4}-\d{2}-\d{2}\.jsonl", _path.name):
    ok("T1: get_log_path() → events-YYYY-MM-DD.jsonl")
else:
    fail("T1: get_log_path() format", f"got {_path}")

# -----------------------------------------------------------------------
# T2 — get_log_path(when=ts) returns the correct local date for that ts
# -----------------------------------------------------------------------
# Use a fixed timestamp: 2024-03-15 12:00 UTC (well within UTC+4 Dubai too)
_ts = 1710500400.0  # 2024-03-15 11:00:00 UTC
_dated = get_log_path(when=_ts)
# Local date may vary by timezone, but the format must hold.
if _re.fullmatch(r"events-\d{4}-\d{2}-\d{2}\.jsonl", _dated.name):
    ok(f"T2: get_log_path(when=ts) returns dated name: {_dated.name}")
else:
    fail("T2: get_log_path(when=ts) format", f"got {_dated.name}")

# -----------------------------------------------------------------------
# T3 — LOGS_DIR is under the test workspace (controlled env)
# -----------------------------------------------------------------------
if str(_mod.LOGS_DIR).startswith(_WORKSPACE):
    ok("T3: LOGS_DIR is under $SUTANDO_WORKSPACE")
else:
    fail("T3: LOGS_DIR under workspace", f"LOGS_DIR={_mod.LOGS_DIR}")

# -----------------------------------------------------------------------
# T4 — log_event creates the JSONL file on first call
# -----------------------------------------------------------------------
path = get_log_path()
path.unlink(missing_ok=True)  # start clean
log_event("test.create")
if path.exists():
    ok("T4: log_event creates the JSONL file on first call")
else:
    fail("T4: log_event creates file", f"path={path}")

# -----------------------------------------------------------------------
# T5 — event has required fields: ts, node, kind
# -----------------------------------------------------------------------
log_event("test.fields")
e = _last_event(get_log_path())
if "ts" in e and "node" in e and "kind" in e:
    ok("T5: event has required fields (ts, node, kind)")
else:
    fail("T5: required fields", f"event={e}")

# -----------------------------------------------------------------------
# T6 — `ts` is a float close to now
# -----------------------------------------------------------------------
log_event("test.ts_check")
e = _last_event(get_log_path())
delta = abs(e["ts"] - time.time())
if isinstance(e["ts"], float) and delta < 5:
    ok(f"T6: event ts is float close to now (delta={delta:.3f}s)")
else:
    fail("T6: event ts", f"ts={e['ts']} delta={delta}")

# -----------------------------------------------------------------------
# T7 — `kind` field matches the kind argument
# -----------------------------------------------------------------------
log_event("bridge.task_written")
e = _last_event(get_log_path())
if e["kind"] == "bridge.task_written":
    ok("T7: event kind matches argument")
else:
    fail("T7: event kind", f"got={e['kind']!r}")

# -----------------------------------------------------------------------
# T8 — extra keyword fields are included in the event
# -----------------------------------------------------------------------
log_event("test.extra_fields", task_id="task-9999", tier="owner", count=7)
e = _last_event(get_log_path())
if e.get("task_id") == "task-9999" and e.get("tier") == "owner" and e.get("count") == 7:
    ok("T8: extra keyword fields included in event")
else:
    fail("T8: extra fields", f"event={e}")

# -----------------------------------------------------------------------
# T9 — non-JSON-serializable values are stringified via repr()
# -----------------------------------------------------------------------
class _Unserializable:
    def __repr__(self):
        return "<Unserializable object>"

log_event("test.repr_fallback", bad=_Unserializable())
e = _last_event(get_log_path())
if e.get("bad") == "<Unserializable object>":
    ok("T9: non-JSON-serializable value stringified via repr()")
else:
    fail("T9: repr fallback", f"bad field={e.get('bad')!r}")

# -----------------------------------------------------------------------
# T10 — multiple log_event calls APPEND (not overwrite)
# -----------------------------------------------------------------------
path = get_log_path()
before_count = len(_events(path))
log_event("test.append_1")
log_event("test.append_2")
log_event("test.append_3")
after_events = _events(path)
if len(after_events) == before_count + 3:
    ok(f"T10: multiple log_event calls append lines ({before_count}→{len(after_events)})")
else:
    fail("T10: append", f"before={before_count} after={len(after_events)}")

# -----------------------------------------------------------------------
# T11 — appended lines are valid JSONL (each line is valid JSON)
# -----------------------------------------------------------------------
path = get_log_path()
lines = path.read_text(encoding="utf-8").strip().splitlines()
bad_lines = []
for i, line in enumerate(lines):
    try:
        json.loads(line)
    except json.JSONDecodeError as exc:
        bad_lines.append(f"line {i}: {exc}")
if not bad_lines:
    ok(f"T11: all {len(lines)} JSONL lines are valid JSON")
else:
    fail("T11: valid JSONL", f"{len(bad_lines)} bad lines: {bad_lines[:2]}")

# -----------------------------------------------------------------------
# T12 — log_event never raises (crash-safe) — even if logs dir is unwritable
# -----------------------------------------------------------------------
orig_logs = _mod.LOGS_DIR
_mod.LOGS_DIR = Path("/nonexistent/no-write-permission/logs")
try:
    log_event("test.crash_safe")  # must not raise
    ok("T12: log_event never raises (crash-safe on bad LOGS_DIR)")
except Exception as exc:
    fail("T12: crash-safe", f"raised: {exc}")
finally:
    _mod.LOGS_DIR = orig_logs

# -----------------------------------------------------------------------
# T13 — log_event with an empty kind string works (kind field is blank str)
# -----------------------------------------------------------------------
log_event("")
e = _last_event(get_log_path())
if e.get("kind") == "":
    ok("T13: log_event with empty kind — kind field is empty string")
else:
    fail("T13: empty kind", f"kind={e.get('kind')!r}")

# -----------------------------------------------------------------------
# T14 — node field is a non-empty string (hostname fallback works)
# -----------------------------------------------------------------------
log_event("test.node_check")
e = _last_event(get_log_path())
if isinstance(e.get("node"), str) and e["node"]:
    ok(f"T14: node field is non-empty string: {e['node']!r}")
else:
    fail("T14: node field", f"node={e.get('node')!r}")

# -----------------------------------------------------------------------
# T15 — event order is preserved (FIFO append)
# -----------------------------------------------------------------------
path = get_log_path()
before_count = len(_events(path))
log_event("test.order_a")
log_event("test.order_b")
log_event("test.order_c")
after = _events(path)
kinds = [e["kind"] for e in after[before_count:]]
if kinds == ["test.order_a", "test.order_b", "test.order_c"]:
    ok("T15: event order is preserved (FIFO append)")
else:
    fail("T15: order", f"kinds={kinds}")

# -----------------------------------------------------------------------
# T16 — ts field is rounded to 3 decimal places (ms precision)
# -----------------------------------------------------------------------
log_event("test.ts_precision")
e = _last_event(get_log_path())
ts_str = str(e["ts"])
# After the decimal point, at most 3 digits
decimal_part = ts_str.split(".")[-1] if "." in ts_str else ""
if len(decimal_part) <= 3:
    ok(f"T16: ts rounded to ≤3 decimal places (got: {e['ts']})")
else:
    fail("T16: ts precision", f"ts={e['ts']!r}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
