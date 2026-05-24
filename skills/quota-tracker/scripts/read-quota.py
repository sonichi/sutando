#!/usr/bin/env python3
"""
Read Claude Code quota state from quota-state.json.

Usage:
  python3 read-quota.py              # human readable
  python3 read-quota.py --json       # machine readable
  python3 read-quota.py --gate       # exit 1 if exhausted
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Canonical home is <workspace>/state/quota-state.json, written by the
# credential proxy. Fallback: the app-bundle proxy (built before the
# workspace-refactor) writes to the skill dir. We accept that path when the
# canonical one is absent — it's fresh data, not a stale leftover. The
# stale-shadowing issue from 2026-05-21 only applies when BOTH exist; the
# canonical always wins in that case.
# NOTE: `.resolve()` follows the ~/.claude/skills symlink into the repo, so the
# path is <repo>/skills/quota-tracker/scripts/read-quota.py — four levels deep.
# Three .parent landed on <repo>/skills (no src/ there); walk up four.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
try:
    from workspace_default import status_read_path, resolve_workspace  # noqa: E402
    _canonical = status_read_path("quota-state.json")
    _workspace: Optional[Path] = Path(str(resolve_workspace()))
except ImportError:
    _canonical = None
    _workspace = None

_skill_dir = Path(__file__).resolve().parent.parent / "quota-state.json"

if _canonical is not None and _canonical.exists():
    QUOTA_FILE = _canonical
elif _skill_dir.exists():
    # App-bundle proxy writes here — accept as fresh data when canonical absent.
    QUOTA_FILE = _skill_dir
else:
    print("No quota-state.json found. Is the credential proxy running?")
    sys.exit(1)

_BURN_HISTORY_FILE: Optional[Path] = (
    _workspace / "state" / "quota-burn-history.json" if _workspace else None
)
_EWMA_ALPHA = 0.3
_MAX_HISTORY = 60  # ~5h at 5min cadence


def _load_history() -> dict:
    if _BURN_HISTORY_FILE is None or not _BURN_HISTORY_FILE.exists():
        return {"samples": [], "ewma_pct_per_min": None}
    try:
        raw = json.loads(_BURN_HISTORY_FILE.read_text())
        # Migrate old list format (pre-#1087) to new dict format
        if isinstance(raw, list):
            return {"samples": [], "ewma_pct_per_min": None}
        return raw
    except (json.JSONDecodeError, OSError):
        return {"samples": [], "ewma_pct_per_min": None}


def _save_history(history: dict) -> None:
    if _BURN_HISTORY_FILE is None:
        return
    try:
        _BURN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BURN_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except OSError:
        pass


def _update_burn_rate(util_5h: float) -> dict:
    """Record utilization sample, update EWMA burn rate, return forecast stats."""
    now = time.time()
    history = _load_history()
    samples: list = history.get("samples", [])
    ewma: Optional[float] = history.get("ewma_pct_per_min")

    if samples:
        prev = samples[-1]
        dt_min = (now - prev["ts"]) / 60.0
        if dt_min > 0:
            delta_pct = (util_5h - prev["util_5h"]) * 100
            # Only count positive consumption; ignore resets and gaps > 30 min
            if delta_pct > 0 and dt_min < 30:
                rate = delta_pct / dt_min
                ewma = rate if ewma is None else (1 - _EWMA_ALPHA) * ewma + _EWMA_ALPHA * rate

    samples.append({"ts": now, "util_5h": util_5h})
    if len(samples) > _MAX_HISTORY:
        samples = samples[-_MAX_HISTORY:]

    _save_history({"samples": samples, "ewma_pct_per_min": ewma})

    remaining_pct = (1 - util_5h) * 100
    if ewma and ewma > 0:
        minutes_to_exhaustion = remaining_pct / ewma
        passes_remaining = minutes_to_exhaustion / 5
    else:
        minutes_to_exhaustion = None
        passes_remaining = None

    return {
        "burn_rate_pct_per_min": round(ewma, 4) if ewma is not None else None,
        "estimated_minutes_to_exhaustion": round(minutes_to_exhaustion) if minutes_to_exhaustion is not None else None,
        "estimated_passes_remaining": round(passes_remaining, 1) if passes_remaining is not None else None,
    }


def main():
    data = json.loads(QUOTA_FILE.read_text())
    headers = data.get("headers", {})

    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    util_5h = float(headers.get("anthropic-ratelimit-unified-5h-utilization", 0))
    util_7d = float(headers.get("anthropic-ratelimit-unified-7d-utilization", 0))
    reset_5h = headers.get("anthropic-ratelimit-unified-5h-reset", "")
    reset_7d = headers.get("anthropic-ratelimit-unified-7d-reset", "")

    burn = _update_burn_rate(util_5h)

    result = {
        "status": status,
        "available": status == "allowed",
        "utilization_5h": util_5h,
        "utilization_7d": util_7d,
        "remaining_5h_pct": round((1 - util_5h) * 100),
        "remaining_7d_pct": round((1 - util_7d) * 100),
        **burn,
    }

    if reset_5h:
        result["reset_5h"] = datetime.fromtimestamp(int(reset_5h)).isoformat()
    if reset_7d:
        result["reset_7d"] = datetime.fromtimestamp(int(reset_7d)).isoformat()

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2))
        return

    if "--gate" in sys.argv:
        sys.exit(0 if result["available"] else 1)

    # Human readable
    print(f"Status: {status}")
    print(f"5h window: {int(util_5h * 100)}% used, {result['remaining_5h_pct']}% remaining")
    if reset_5h:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_5h)).strftime('%H:%M %b %d')}")
    print(f"7d window: {int(util_7d * 100)}% used, {result['remaining_7d_pct']}% remaining")
    if reset_7d:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_7d)).strftime('%H:%M %b %d')}")
    if burn["burn_rate_pct_per_min"] is not None:
        rate_str = f"{burn['burn_rate_pct_per_min']:.3f}%/min"
        passes_str = (
            f"  (~{burn['estimated_passes_remaining']:.0f} passes left)"
            if burn["estimated_passes_remaining"] is not None else ""
        )
        print(f"Burn rate: {rate_str}{passes_str}")


if __name__ == "__main__":
    main()
