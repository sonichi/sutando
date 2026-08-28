#!/usr/bin/env python3
"""health-check's two gateway AGE helpers re-parse the same sidecar the shared
verdict owner reads, and did so with their own inline numeric handling.

`_gateway_serving` delegates to `gateway_serving.read_verdict`, so a JSON-valid
but unrepresentable number degrades to "no opinion" there. The age helpers did
not: `_gateway_status_stale_age_s` computed `now - ts` and
`_gateway_last_ok_age_h` computed `float(last)`, both of which raise
OverflowError for an arbitrarily large JSON integer — aborting the whole
health-check run and hiding every other diagnosis. NaN/inf were worse than a
raise in the last_ok path: they collapsed to 0.0 hours, i.e. "just polled".

Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import time
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES = []
HUGE = 10 ** 400
NOW = 1_000_000.0


def ok(name, cond, detail=""):
    print(f"{'  ok  ' if cond else '  FAIL '}{name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def _sidecar(record) -> Path:
    p = Path(tempfile.mkdtemp()) / "gateway-status.json"
    p.write_text(json.dumps(record))
    return p


def _call(fn, record):
    """(value, raised) for one helper over a real sidecar file."""
    try:
        return fn(_sidecar(record), NOW), None
    except Exception as exc:  # noqa: BLE001 - the raise IS the defect under test
        return None, f"{type(exc).__name__}: {exc}"


print("=== control: the input really is dangerous ===")
_raised = None
try:
    float(HUGE)
except OverflowError as exc:
    _raised = str(exc)
ok("float(10**400) raises, so a guard here is load-bearing", _raised is not None,
   "float() accepted it — this suite would pass without the fix")

print("\n=== _gateway_status_stale_age_s ===")
for label, ts in (("huge int", HUGE), ("negative huge int", -HUGE),
                  ("ordinary negative", -1), ("zero-adjacent negative", -0.5),
                  ("NaN", float("nan")), ("+inf", float("inf"))):
    v, raised = _call(hc._gateway_status_stale_age_s, {"connected": True, "ts": ts, "last_ok_ts": 1})
    ok(f"{label} ts -> no opinion, no raise", raised is None and v is None, raised or f"returned {v!r}")

# POSITIVE controls: a helper that answered None unconditionally would pass
# every case above. These fail unless it still measures a usable record.
v, raised = _call(hc._gateway_status_stale_age_s,
                  {"connected": True, "ts": NOW - 600, "last_ok_ts": 1})
ok("positive control: a genuinely stale ts still reports its age",
   raised is None and v is not None and abs(v - 600) < 1, raised or f"returned {v!r}")
v, raised = _call(hc._gateway_status_stale_age_s, {"connected": True, "ts": NOW, "last_ok_ts": 1})
ok("positive control: a fresh ts still reports None", raised is None and v is None,
   raised or f"returned {v!r}")

print("\n=== _gateway_last_ok_age_h ===")
for label, last in (("huge int", HUGE), ("negative huge int", -HUGE),
                    ("ordinary negative", -1), ("zero-adjacent negative", -0.5),
                    ("NaN", float("nan")), ("+inf", float("inf")), ("-inf", float("-inf"))):
    v, raised = _call(hc._gateway_last_ok_age_h, {"connected": True, "ts": NOW, "last_ok_ts": last})
    ok(f"{label} last_ok_ts -> no opinion, no raise", raised is None and v is None,
       raised or f"returned {v!r}")

v, raised = _call(hc._gateway_last_ok_age_h,
                  {"connected": True, "ts": NOW, "last_ok_ts": NOW - 7200})
ok("positive control: a usable last_ok_ts still reports its age in hours",
   raised is None and v is not None and abs(v - 2.0) < 0.01, raised or f"returned {v!r}")

# NaN/inf were not merely a raise risk here: max(0.0, nan) is 0.0 and
# max(0.0, -inf) is 0.0, so both read as "polled just now".
ok("control: the pre-fix arithmetic really did read NaN as 0.0 hours",
   max(0.0, (NOW - float("nan")) / 3600.0) == 0.0 and math.isnan(float("nan")))

# -10**400 degrades because CONVERSION overflows, not because it is negative, so the
# overflow rows alone never exercised the sign rule. These do.
ok("control: the negative-huge case really is decided by overflow, not by sign",
   _call(hc._gateway_status_stale_age_s,
         {"connected": True, "ts": -HUGE, "last_ok_ts": 1})[1] is None)

print("\n=== full probe: check_gateway_bridge() over a real sidecar ===")


def _probe(record):
    """check_gateway_bridge() with a running process and NO age-helper mocks."""
    path = _sidecar(record)

    def _pgrep(cmd, **kw):
        r = unittest.mock.MagicMock()
        r.returncode, r.stdout = 0, "4242\n"
        return r

    base = {k: v for k, v in hc.os.environ.items() if k not in ("AG2_REMOTE_TOKEN",)}
    base["REMOTE_TASK_TOKEN"] = "tok"
    with unittest.mock.patch.dict(hc.os.environ, base, clear=True), \
         unittest.mock.patch.object(hc, "claude_home_path",
                                    return_value=Path(tempfile.mkdtemp()) / "nope.env"), \
         unittest.mock.patch.object(hc, "status_read_path", lambda *a, **k: path), \
         unittest.mock.patch.object(hc, "_gateway_lock_pids", lambda: {}), \
         unittest.mock.patch.object(hc.subprocess, "run",
                                    unittest.mock.Mock(side_effect=_pgrep)):
        try:
            return hc.check_gateway_bridge(), None
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"


# The probe reads the real clock, so these records carry a real `ts`: a fresh
# record is what reaches the age helpers at all.
_REAL = time.time()
for label, record in (
    ("huge ts", {"ts": HUGE, "connected": True, "last_ok_ts": _REAL}),
    ("huge last_ok_ts", {"ts": _REAL, "connected": True, "last_ok_ts": HUGE}),
    ("both huge", {"ts": HUGE, "connected": True, "last_ok_ts": HUGE}),
):
    r, raised = _probe(record)
    ok(f"{label}: the whole probe degrades instead of aborting the run",
       raised is None and isinstance(r, dict) and r.get("status") in ("ok", "warn", "fail"),
       raised or f"returned {r!r}")

for label, record in (
    ("negative ts", {"ts": -1, "connected": True, "last_ok_ts": _REAL}),
    ("negative last_ok_ts", {"ts": _REAL, "connected": True, "last_ok_ts": -1}),
):
    r, raised = _probe(record)
    ok(f"{label}: the full probe degrades rather than reporting a fabricated age",
       raised is None and isinstance(r, dict) and r.get("status") in ("ok", "warn", "fail"),
       raised or f"returned {r!r}")

r, raised = _probe({"ts": _REAL, "connected": True, "last_ok_ts": _REAL})
ok("positive control: a healthy sidecar still probes clean",
   raised is None and isinstance(r, dict) and r.get("status") == "ok",
   raised or f"returned {r!r}")

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}):")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("All gateway age-helper overflow controls passed.")
