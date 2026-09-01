#!/usr/bin/env python3
"""`connected` alone must not read as serving: a lane that never completed a poll
carries connected=true with last_ok_ts null. Pins both polarities."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

NOW = time.time()
CASES = [
    ("connected + a real last_ok_ts is serving", {"ts": NOW, "connected": True, "last_ok_ts": NOW - 5}, True),
    ("connected + last_ok_ts null is NOT serving", {"ts": NOW, "connected": True, "last_ok_ts": None}, False),
    ("connected + no last_ok_ts key is NOT serving", {"ts": NOW, "connected": True}, False),
    ("connected + bool last_ok_ts is NOT serving", {"ts": NOW, "connected": True, "last_ok_ts": True}, False),
    ("not connected stays False", {"ts": NOW, "connected": False, "last_ok_ts": NOW - 9}, False),
    ("a stale sidecar stays None (no opinion)", {"ts": NOW - 9999, "connected": True, "last_ok_ts": NOW}, None),
]

failures = []
for name, doc, want in CASES:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        path = Path(fh.name)
    got = hc._gateway_serving(path=path, now=NOW)
    if got is not want:
        failures.append(f"{name}: got {got!r}, want {want!r}")

# The never-polled lane must reach the warn branch with an honest age, not ok.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"ts": NOW, "connected": True, "last_ok_ts": None}, fh)
    never = Path(fh.name)
if hc._gateway_last_ok_age_h(path=never) is not None:
    failures.append("null last_ok_ts should yield an UNKNOWN age, not a number")

if failures:
    for f in failures:
        print(f"FAIL: {f}")
    sys.exit(1)
print(f"ok - {len(CASES)} serving cases + the UNKNOWN-age path")
