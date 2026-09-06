#!/usr/bin/env python3
"""The defensive branches in `delivery.readiness` — the ones that decide what
happens when the counter or history file is corrupt.

Every one of these is an `except Exception` that fails in a chosen direction:
refuse to stamp, reuse a reservation, treat a bad history as zero. None was
exercised, so the direction was asserted only by the comment next to it.

Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("rr", REPO / "src" / "delivery" / "readiness.py")
rr = importlib.util.module_from_spec(_spec)
sys.modules["rr"] = rr
_spec.loader.exec_module(rr)

FAILURES = []


def ok(name, cond, detail=""):
    print(f"{'  ok  ' if cond else '  FAIL '}{name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def _state(**files) -> Path:
    d = Path(tempfile.mkdtemp())
    for n, v in files.items():
        (d / n).write_text(v if isinstance(v, str) else json.dumps(v))
    return d


print("=== _stamp_exempt: an unparseable body must NOT be stamped ===")
_real = rr._load_parse_markers
try:
    def _boom(text):
        raise RuntimeError("grammar blew up")
    rr._load_parse_markers = lambda: _boom
    ok("a body the marker parser cannot read is treated as exempt",
       rr._stamp_exempt("anything") is True)
finally:
    rr._load_parse_markers = _real
ok("control: with the parser working, a plain body is NOT exempt",
   rr._stamp_exempt("the reply") is False)

print("\n=== _release_reservation: corrupt or absent state is swallowed ===")
s = _state(**{"task-counter.json": {"date": "20260828", "count": 3}})
rr._release_reservation(s, "task-x.txt")          # no `pending` key at all
ok("a counter with no pending map returns early, file untouched",
   json.loads((s / "task-counter.json").read_text()) == {"date": "20260828", "count": 3})

s = _state(**{"task-counter.json": {"pending": {"other.txt": "20260828-001"}}})
rr._release_reservation(s, "task-x.txt")          # name not in pending
ok("releasing a name that holds no reservation leaves the others intact",
   json.loads((s / "task-counter.json").read_text())["pending"] == {"other.txt": "20260828-001"})

s = _state(**{"task-counter.json": "{ not json"})
_crash = None
try:
    rr._release_reservation(s, "task-x.txt")
except Exception as exc:                          # noqa: BLE001
    _crash = f"{type(exc).__name__}: {exc}"
ok("a corrupt counter is swallowed — a lingering reservation is reused, not double-spent",
   _crash is None, _crash or "")

s = _state(**{"task-counter.json": {"pending": {"task-x.txt": "20260828-007"}}})
rr._release_reservation(s, "task-x.txt")
ok("control: a real reservation IS released",
   "pending" not in json.loads((s / "task-counter.json").read_text()))

print("\n=== _reconcile_history: every malformed shape has a chosen direction ===")
ok("a tid whose sequence is not a number reconciles to False",
   rr._reconcile_history(_state(), "20260828-abc") is False)
ok("a tid with no sequence at all reconciles to False",
   rr._reconcile_history(_state(), "20260828") is False)

s = _state(**{"task-completions-daily.json": []})   # a list, not a mapping
ok("a history that is not a mapping is replaced, not trusted",
   rr._reconcile_history(s, "20260828-005") is True)
ok("and the day's floor is then written from the tid",
   json.loads((s / "task-completions-daily.json").read_text()).get("20260828") == 5)

s = _state(**{"task-completions-daily.json": "{ not json"})
ok("an unparseable history is replaced rather than aborting the stamp",
   rr._reconcile_history(s, "20260828-004") is True)

s = _state(**{"task-completions-daily.json": {"20260828": "not-a-number"}})
ok("a non-numeric day count reads as zero, so the floor still rises",
   rr._reconcile_history(s, "20260828-006") is True)
ok("and the corrupt value is overwritten with the tid's sequence",
   json.loads((s / "task-completions-daily.json").read_text()).get("20260828") == 6)

s = _state(**{"task-completions-daily.json": {"20260828": 9}})
ok("control: a day already ABOVE the tid is left alone (no floor lowering)",
   rr._reconcile_history(s, "20260828-002") is True
   and json.loads((s / "task-completions-daily.json").read_text())["20260828"] == 9)

s = Path(tempfile.mkdtemp()) / "nope" / "deeper"   # parent does not exist
ok("an unwritable history directory reconciles to False rather than raising",
   rr._reconcile_history(s, "20260828-003") is False)

print("\n=== _alloc_locked: a corrupt counter starts a fresh day ===")
s = _state(**{"task-counter.json": "{ not json"})
tid = rr._alloc_locked(s)
ok("an unparseable counter allocates rather than failing", isinstance(tid, str) and tid.endswith("-001"),
   f"got {tid!r}")
s = _state(**{"task-counter.json": [1, 2, 3]})     # valid JSON, wrong type
tid = rr._alloc_locked(s)
ok("a counter of the wrong TYPE is also treated as a fresh day",
   isinstance(tid, str) and tid.endswith("-001"), f"got {tid!r}")
s = _state(**{"task-counter.json": {"date": rr.date.today().strftime("%Y%m%d"), "count": 4}})
tid = rr._alloc_locked(s)
ok("control: a healthy counter continues its sequence, it does not reset",
   isinstance(tid, str) and tid.endswith("-005"), f"got {tid!r}")

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}):")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("All readiness failure-path controls passed.")
