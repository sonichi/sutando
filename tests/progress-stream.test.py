#!/usr/bin/env python3
"""Tests for src/progress_stream.py — the pure progress-streaming helpers.

Run directly: `python3 tests/progress-stream.test.py` (no pytest dependency,
matching the repo's other *.test.py suites).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import progress_stream as ps  # noqa: E402

_fails = []


def check(name, cond):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}")
        _fails.append(name)


# --- stream_enabled (feature flag, default OFF) ---
os.environ.pop("SUTANDO_PROGRESS_STREAM", None)
check("flag default OFF", ps.stream_enabled() is False)
os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
check("flag on when =1", ps.stream_enabled() is True)
os.environ["SUTANDO_PROGRESS_STREAM"] = "true"
check("flag off when !=1 (strict)", ps.stream_enabled() is False)
os.environ.pop("SUTANDO_PROGRESS_STREAM", None)

# --- should_stream_task (owner-only) ---
check("owner streams", ps.should_stream_task("owner") is True)
check("owner streams (caps/space)", ps.should_stream_task("  Owner ") is True)
check("None tier streams (legacy owner)", ps.should_stream_task(None) is True)
check("team does NOT stream", ps.should_stream_task("team") is False)
check("other does NOT stream", ps.should_stream_task("other") is False)

# --- read_core_status (never raises) ---
with tempfile.TemporaryDirectory() as d:
    sd = Path(d)
    check("missing file -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("")
    check("empty file -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("{not json")
    check("malformed -> None (no raise)", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("[1,2,3]")
    check("non-dict json -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text(json.dumps({"status": "running", "step": "scanning"}))
    got = ps.read_core_status(sd)
    check("valid -> dict", isinstance(got, dict) and got.get("step") == "scanning")

# read_core_status legacy fallback: state_dir/../core-status.json (un-migrated)
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    state = root / "state"
    state.mkdir()
    # only the legacy workspace-root file exists, not state/core-status.json
    (root / "core-status.json").write_text(json.dumps({"status": "running", "step": "legacy"}))
    got = ps.read_core_status(state)
    check("legacy root fallback read", isinstance(got, dict) and got.get("step") == "legacy")
    # primary takes precedence over legacy when both exist
    (state / "core-status.json").write_text(json.dumps({"status": "running", "step": "primary"}))
    got2 = ps.read_core_status(state)
    check("primary wins over legacy", got2.get("step") == "primary")

# --- current_step ---
check("idle -> None (no narration)", ps.current_step({"status": "idle", "step": "x"}) is None)
check("running+step -> step", ps.current_step({"status": "running", "step": "scanning"}) == "scanning")
check("running+blank step -> None", ps.current_step({"status": "running", "step": "   "}) is None)
check("running+non-str step -> None", ps.current_step({"status": "running", "step": 42}) is None)
check("None status dict -> None", ps.current_step(None) is None)
check("missing step -> None", ps.current_step({"status": "running"}) is None)

# --- thresholds / rate-limit / expiry ---
check("no placeholder before threshold", ps.should_post_placeholder(3, 8) is False)
check("placeholder at threshold", ps.should_post_placeholder(8, 8) is True)
check("placeholder past threshold", ps.should_post_placeholder(20, 8) is True)
check("no edit within interval", ps.should_edit(10.0, 8.0, 4) is False)
check("edit after interval", ps.should_edit(12.5, 8.0, 4) is True)
check("not expired before max age", ps.placeholder_expired(100, 1800) is False)
check("expired at max age", ps.placeholder_expired(1800, 1800) is True)

# --- format_progress ---
check("format includes step + secs", ps.format_progress("scanning Gmail", 12) == "⏳ scanning Gmail (12s)")
check("format None step -> working", ps.format_progress(None, 9) == "⏳ working… (9s)")
check("format blank step -> working", ps.format_progress("   ", 9) == "⏳ working… (9s)")
check("format negative elapsed clamps to 0", ps.format_progress("x", -5) == "⏳ x (0s)")
long_step = "z" * 500
out = ps.format_progress(long_step, 3, max_len=180)
check("format truncates long step", len(out) < 220 and out.endswith("(3s)") and "…" in out)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed")
