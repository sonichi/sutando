#!/usr/bin/env python3
"""Regression tests for the Codex proactive-loop quota gate."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
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

# qingyun CR #2676 (round 3): json.loads() accepts NaN/Infinity tokens, so a
# non-finite window field is a float that passes isinstance but then crashes
# int()/comparisons. It must drop out (fail closed), never raise.
check("NaN window_minutes is rejected, not crashed on",
      gate._weekly_window(json.loads('{"primary":{"used_percent":10,"window_minutes":NaN}}')) is None)
check("Infinity window_minutes is rejected",
      gate._weekly_window(json.loads('{"primary":{"used_percent":10,"window_minutes":Infinity}}')) is None)
check("NaN used_percent is rejected",
      gate._weekly_window(json.loads('{"primary":{"used_percent":NaN,"window_minutes":10080}}')) is None)
check("_finite_number rejects NaN/inf/bool, accepts real numbers",
      not gate._finite_number(float("nan")) and not gate._finite_number(float("inf"))
      and not gate._finite_number(True) and gate._finite_number(10) and gate._finite_number(3.5))

# ---- _codex_home: env override + config-dir resolution + fallback ----
_saved_home = os.environ.get("CODEX_HOME")
os.environ["CODEX_HOME"] = "/tmp/x-codex-home"
check("_codex_home honors CODEX_HOME env", str(gate._codex_home()) == "/tmp/x-codex-home")
os.environ.pop("CODEX_HOME", None)
_saved_mod = sys.modules.get("sutando_config")
_fake = types.ModuleType("sutando_config")
_fake.resolve_core_config_dirs = lambda repo: [
    {"type": "claude", "value": "/nope"}, {"type": "codex", "value": "/tmp/codex-cfg"}]
sys.modules["sutando_config"] = _fake
check("_codex_home reads the codex config dir", str(gate._codex_home()) == "/tmp/codex-cfg")
def _raise(repo):
    raise RuntimeError("boom")
_fake2 = types.ModuleType("sutando_config")
_fake2.resolve_core_config_dirs = _raise
sys.modules["sutando_config"] = _fake2
check("_codex_home falls back to Path() on resolver error", isinstance(gate._codex_home(), Path))
if _saved_mod is not None:
    sys.modules["sutando_config"] = _saved_mod
else:
    sys.modules.pop("sutando_config", None)
if _saved_home is not None:
    os.environ["CODEX_HOME"] = _saved_home

# ---- _rate_limits: direct / info-nested / absent / non-dict ----
check("_rate_limits None for non-dict payload", gate._rate_limits("x") is None)
check("_rate_limits reads info.rate_limits nesting",
      gate._rate_limits({"info": {"rate_limits": {"primary": {}}}}) == {"primary": {}})
check("_rate_limits None when rate_limits absent", gate._rate_limits({"foo": 1}) is None)

# ---- _snapshots: missing dir, stale skip, bad json, no-limits, numeric/bad ts ----
with tempfile.TemporaryDirectory() as td2:
    root2 = Path(td2)
    check("_snapshots empty when no sessions dir", gate._snapshots(root2) == [])
    sess = root2 / "sessions" / "s"
    sess.mkdir(parents=True)
    stale_f = sess / "stale.jsonl"
    stale_f.write_text(json.dumps({
        "timestamp": "2020-01-01T00:00:00Z",
        "payload": {"rate_limits": {"limit_id": "old", "primary": {"used_percent": 1, "window_minutes": 10080}}},
    }) + "\n", encoding="utf-8")
    os.utime(stale_f, (0, 0))  # mtime at epoch -> older than the 14d cutoff
    mix = sess / "mix.jsonl"
    mix.write_text(
        "{ not valid json\n"
        + json.dumps({"payload": {"no": "limits"}}) + "\n"
        + json.dumps({"timestamp": 1785000000,
                      "payload": {"rate_limits": {"limit_id": "numts",
                                                  "primary": {"used_percent": 5, "window_minutes": 10080}}}}) + "\n"
        + json.dumps({"timestamp": "not-a-date",
                      "payload": {"rate_limits": {"limit_id": "badts",
                                                  "primary": {"used_percent": 5, "window_minutes": 10080}}}}) + "\n",
        encoding="utf-8")
    snaps = gate._snapshots(root2)
    ids = {s[1].get("limit_id") for s in snaps}
    check("_snapshots skips the stale file", "old" not in ids)
    check("_snapshots parses numeric + bad timestamps, skips bad-json/no-limits lines",
          {"numts", "badts"} <= ids and len(snaps) == 2)

# ---- read_quota tier branches: FULL / LIGHT / no-weekly-window ----
with tempfile.TemporaryDirectory() as tf:
    rf = Path(tf); nowf = time.time(); write_snapshot(rf, 5, stamp=nowf)  # remaining 95
    check("remaining >20 -> FULL", gate.read_quota(rf, now=nowf)["tier"] == "FULL")
with tempfile.TemporaryDirectory() as tl:
    rl = Path(tl); nowl = time.time(); write_snapshot(rl, 98, stamp=nowl)  # remaining 2
    check("remaining <5 -> LIGHT", gate.read_quota(rl, now=nowl)["tier"] == "LIGHT")
with tempfile.TemporaryDirectory() as tsh:
    rsh = Path(tsh); nowsh = time.time()
    sp = rsh / "sessions" / "f" / "short.jsonl"; sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "timestamp": datetime.fromtimestamp(nowsh, timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {"rate_limits": {"primary": {"used_percent": 10, "window_minutes": 300}}},
    }) + "\n", encoding="utf-8")
    res = gate.read_quota(rsh, now=nowsh)
    check("a snapshot with only a short window -> unavailable/LIGHT",
          res["tier"] == "LIGHT" and not res["available"])

# ---- main(): --json, text-available, text-unavailable ----
def _run_main(argv, codex_home):
    saved_argv, saved_home = sys.argv, os.environ.get("CODEX_HOME")
    sys.argv = argv
    os.environ["CODEX_HOME"] = str(codex_home)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = gate.main()
    finally:
        sys.argv = saved_argv
        if saved_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = saved_home
    return rc, buf.getvalue()

with tempfile.TemporaryDirectory() as tm:
    rm = Path(tm); write_snapshot(rm, 40, stamp=time.time())  # fresh, available
    rc, out = _run_main(["g", "--json"], rm)
    check("main --json returns 0 and emits JSON", rc == 0 and '"available": true' in out)
    rc, out = _run_main(["g"], rm)
    check("main text mode prints the gate tier", "gate=" in out and "remaining" in out)
with tempfile.TemporaryDirectory() as tu:
    rc, out = _run_main(["g"], Path(tu))  # empty -> unavailable
    check("main text mode reports unavailable when no telemetry", "unavailable" in out)

if failures:
    raise SystemExit(f"{len(failures)} failure(s): {', '.join(failures)}")
print("\nall tests passed")
