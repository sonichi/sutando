#!/usr/bin/env python3
"""Regression tests for the Codex proactive-loop quota gate."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/proactive-loop/scripts/codex-quota-gate.py"
spec = importlib.util.spec_from_file_location("codex_quota_gate", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

failures = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}")
        failures.append(label)


def write_snapshot(root: Path, used: float, *, lane: str = "codex", stamp: Optional[float] = None) -> None:
    path = root / "sessions" / "fixture" / f"{lane}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    event = {
        "timestamp": (now if stamp is None else datetime.fromtimestamp(stamp, timezone.utc)).isoformat().replace("+00:00", "Z"),
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": lane,
                "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 9999999999},
            },
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    now = time.time()
    write_snapshot(root, 89, stamp=now)
    write_snapshot(root, 0, lane="codex_bengalfox", stamp=now)
    result = gate.read_quota(root, now=now)
    check("weekly lanes are read", len(result["limits"]) == 2)
    check("worst lane determines remaining", result["remaining_percent"] == 11.0)
    check("10% remaining is MEDIUM", result["tier"] == "MEDIUM")

    stale = root / "sessions" / "stale"
    stale.mkdir(parents=True, exist_ok=True)
    old = now - gate.STALE_AFTER_SECONDS - 1
    write_snapshot(root, 89, lane="only-stale", stamp=old)
    # A root with only stale telemetry fails closed, while a fresh lane keeps
    # the stale lane in the conservative worst-case bound.
    only_stale = Path(td) / "only-stale-root"
    write_snapshot(only_stale, 89, lane="codex", stamp=old)
    result = gate.read_quota(only_stale, now=now)
    check("entirely stale telemetry fails closed", result["tier"] == "LIGHT" and not result["available"])

    # qingyun CR #2676: the weekly window is not always `primary`. When a shorter
    # (300-min) window occupies `primary` and the 10,080-min weekly window is in
    # `secondary`, the gate must still read the weekly telemetry — not drop the
    # snapshot and report unavailable/LIGHT.
    dual = Path(td) / "dual-window-root"
    dpath = dual / "sessions" / "fixture" / "codex.jsonl"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text(json.dumps({
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 111},
                "secondary": {"used_percent": 90, "window_minutes": 10080, "resets_at": 222},
            },
        },
    }) + "\n", encoding="utf-8")
    result = gate.read_quota(dual, now=now)
    check("weekly window read from secondary when primary is a short window",
          result["available"] and len(result["limits"]) == 1)
    check("secondary weekly used_percent (90) drives remaining, not primary (25)",
          result["remaining_percent"] == 10.0)
    check("10% remaining from the weekly secondary is MEDIUM", result["tier"] == "MEDIUM")
    check("the weekly window's resets_at is surfaced (222, not the 300-min 111)",
          result["limits"][0]["resets_at"] == 222)

# _weekly_window branch coverage: every non-weekly shape degrades to None so the
# snapshot is dropped (and the gate fails closed) rather than mis-read.
check("no weekly window when only a short window is present",
      gate._weekly_window({"primary": {"used_percent": 25, "window_minutes": 300}}) is None)
check("non-dict window is ignored",
      gate._weekly_window({"primary": "nope",
                           "secondary": {"used_percent": 90, "window_minutes": 10080}})
      is not None)
check("non-numeric used_percent is ignored",
      gate._weekly_window({"primary": {"used_percent": "x", "window_minutes": 10080}}) is None)
check("boolean used_percent is not treated as numeric",
      gate._weekly_window({"primary": {"used_percent": True, "window_minutes": 10080}}) is None)
check("missing/non-numeric window_minutes is ignored",
      gate._weekly_window({"primary": {"used_percent": 10}}) is None)
check("empty snapshot yields no weekly window", gate._weekly_window({}) is None)
check("longest qualifying window wins when both are weekly-length",
      gate._weekly_window({
          "primary": {"used_percent": 10, "window_minutes": 10080},
          "secondary": {"used_percent": 20, "window_minutes": 20160},
      })["window_minutes"] == 20160)

if failures:
    raise SystemExit(f"{len(failures)} failure(s): {', '.join(failures)}")
print("\nall tests passed")
