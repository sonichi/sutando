#!/usr/bin/env python3
"""Behavioral tests for `src/screen-capture-server.py` notification debounce logic.

Tests the _notify_capture() debounce mechanism — the only function with testable
non-network logic. Guards against:
  - NOTIFY_ENABLED=False → no-op
  - Second call within NOTIFY_DEBOUNCE_S → skipped (no spam)
  - After debounce window → notification allowed again

Actual notification delivery (osascript) is bypassed by monkeypatching
_notify_capture_blocking.

Run: python3 tests/screen-capture-debounce.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader
# -----------------------------------------------------------------------
# Ensure notify is enabled so debounce logic actually runs
os.environ["SUTANDO_CAPTURE_NOTIFY"] = "1"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("screen_capture_server", "screen-capture-server"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location(
    "screen_capture_server", _SRC / "screen-capture-server.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_notify_capture = _mod._notify_capture
NOTIFY_DEBOUNCE_S = _mod.NOTIFY_DEBOUNCE_S

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


class _CallTracker:
    """Monkeypatches _notify_capture_blocking with a counter."""
    def __enter__(self):
        self.call_count = 0
        self._orig = _mod._notify_capture_blocking
        def _stub():
            self.call_count += 1
        _mod._notify_capture_blocking = _stub
        return self
    def __exit__(self, *_):
        _mod._notify_capture_blocking = self._orig


# -----------------------------------------------------------------------
# NOTIFY_DEBOUNCE_S constant
# -----------------------------------------------------------------------

# T1 — NOTIFY_DEBOUNCE_S is 5.0 seconds
if NOTIFY_DEBOUNCE_S == 5.0:
    ok("T1: NOTIFY_DEBOUNCE_S == 5.0 (debounce window)")
else:
    fail("T1: NOTIFY_DEBOUNCE_S", f"got {NOTIFY_DEBOUNCE_S}")

# -----------------------------------------------------------------------
# _notify_capture — NOTIFY_ENABLED gate
# -----------------------------------------------------------------------

# T2 — NOTIFY_ENABLED=False → _notify_capture is a no-op (no thread spawned)
with _CallTracker() as tracker:
    _mod.NOTIFY_ENABLED = False
    _mod._last_notify_ts = 0.0
    _notify_capture()
    time.sleep(0.05)  # let any daemon thread complete
    _mod.NOTIFY_ENABLED = True
if tracker.call_count == 0:
    ok("T2: NOTIFY_ENABLED=False → _notify_capture is a no-op (blocking fn not called)")
else:
    fail("T2: notify disabled no-op", f"call_count={tracker.call_count}")

# T3 — first call when notify enabled → blocking fn called (notification fires)
with _CallTracker() as tracker:
    _mod.NOTIFY_ENABLED = True
    _mod._last_notify_ts = 0.0  # reset debounce
    _notify_capture()
    time.sleep(0.1)  # allow daemon thread to execute
if tracker.call_count == 1:
    ok("T3: first _notify_capture call → blocking fn called once")
else:
    fail("T3: first call fires", f"call_count={tracker.call_count}")

# T4 — second call immediately after first → debounced (blocking fn not called again)
with _CallTracker() as tracker:
    _mod.NOTIFY_ENABLED = True
    _mod._last_notify_ts = 0.0
    _notify_capture()
    time.sleep(0.05)
    before_second = tracker.call_count
    _notify_capture()
    time.sleep(0.05)
    after_second = tracker.call_count
if before_second == 1 and after_second == 1:
    ok("T4: second call within debounce window → skipped (no duplicate notification)")
else:
    fail("T4: debounce prevents duplicate", f"before={before_second} after={after_second}")

# T5 — after debounce window expires → notification allowed again
with _CallTracker() as tracker:
    _mod.NOTIFY_ENABLED = True
    # Simulate last notify was NOTIFY_DEBOUNCE_S + 1 seconds ago
    _mod._last_notify_ts = time.time() - (NOTIFY_DEBOUNCE_S + 1)
    _notify_capture()
    time.sleep(0.1)
if tracker.call_count == 1:
    ok(f"T5: after debounce window ({NOTIFY_DEBOUNCE_S}s) expires → notification fires again")
else:
    fail("T5: post-debounce fires", f"call_count={tracker.call_count}")

# T6 — _last_notify_ts updated after first call (prevents immediate re-trigger)
with _CallTracker() as tracker:
    _mod.NOTIFY_ENABLED = True
    _mod._last_notify_ts = 0.0
    ts_before = time.time()
    _notify_capture()
    time.sleep(0.05)
    ts_after = _mod._last_notify_ts
if ts_after >= ts_before:
    ok("T6: _last_notify_ts updated after notification fires (debounce timestamp advances)")
else:
    fail("T6: timestamp updated", f"ts_before={ts_before:.3f} ts_after={ts_after:.3f}")

# T7 — no crash when blocking fn raises an exception (fail-safe design)
orig = _mod._notify_capture_blocking
def _raising():
    raise RuntimeError("osascript not found")
_mod._notify_capture_blocking = _raising
_mod._last_notify_ts = 0.0
_mod.NOTIFY_ENABLED = True
try:
    _notify_capture()
    time.sleep(0.1)
    ok("T7: exception in blocking fn does not propagate to caller (daemon thread absorbs it)")
except Exception as e:
    fail("T7: exception absorbed", f"raised {e}")
finally:
    _mod._notify_capture_blocking = orig

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
