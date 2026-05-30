#!/usr/bin/env python3
"""
Read Claude Code quota state from quota-state.json.

Usage:
  python3 read-quota.py              # human readable
  python3 read-quota.py --json       # machine readable
  python3 read-quota.py --gate       # exit 1 if exhausted

Burn-rate tracking (closes #1087):
  On each human/json read, tracks per-5min utilization delta via an EWMA
  (alpha=0.3) stored in state/quota-burn-history.json. Outputs:
    Burn rate: X.X%/pass (N samples)
    Est. passes left: N (~Nm)
  Skips the sample if a 5h reset occurred (util dropped) or the gap is
  outside the 2min–2h window.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Canonical (and only) home is <workspace>/state/quota-state.json, written by
# the credential proxy. The skill-dir / cwd fallbacks were removed: a stale
# leftover quota-state.json under skills/quota-tracker/ silently shadowed the
# fresh file and froze the dashboard for ~12h (2026-05-21). One path, one
# source of truth — if it's missing, say so rather than read a stale copy.
# NOTE: `.resolve()` follows the ~/.claude/skills symlink into the repo, so the
# path is <repo>/skills/quota-tracker/scripts/read-quota.py — four levels deep.
# Three .parent landed on <repo>/skills (no src/ there), so the workspace_default
# import silently failed (→ except below → "not found") and quota read as missing
# regardless of where the proxy wrote. Walk up four to reach <repo>/src.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
try:
    from workspace_default import status_read_path  # noqa: E402
    _canonical = status_read_path("quota-state.json")
    _burn_history_path = status_read_path("quota-burn-history.json")
except ImportError:
    _canonical = None
    _burn_history_path = None

if _canonical is not None and _canonical.exists():
    QUOTA_FILE = _canonical
else:
    print("No quota-state.json found. Is the credential proxy running?")
    sys.exit(1)

BURN_HISTORY_FILE: Path | None = _burn_history_path

# EWMA smoothing factor. 0.3 = ~3-sample half-life, responsive without noise.
_EWMA_ALPHA = 0.3
# Sample inclusion window: skip deltas from outside [MIN_GAP_S, MAX_GAP_S].
_MIN_GAP_S = 120     # 2 min — same-pass double-reads shouldn't count
_MAX_GAP_S = 7200    # 2 h — stale gap yields unreliable per-pass rate


def _load_burn_history() -> dict:
    if not BURN_HISTORY_FILE:
        return {}
    try:
        return json.loads(BURN_HISTORY_FILE.read_text())
    except Exception:
        return {}


def _save_burn_history(h: dict) -> None:
    if not BURN_HISTORY_FILE:
        return
    try:
        BURN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BURN_HISTORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.rename(BURN_HISTORY_FILE)
    except Exception:
        pass


def _update_burn_rate(current_util_5h: float) -> dict | None:
    """Update burn-rate EWMA with the current 5h utilization sample.

    Returns a dict with burn_rate_pct_per_pass and estimated_passes_left
    if enough history exists, else None.
    """
    now = time.time()
    h = _load_burn_history()
    last_ts = h.get("last_read_ts")
    last_util = h.get("last_util_5h")
    ewma = h.get("burn_rate_5h_ewma")
    samples = h.get("burn_samples", 0)

    new_h = dict(h)
    new_h["last_read_ts"] = now
    new_h["last_util_5h"] = current_util_5h
    new_h["schema_version"] = 1

    result = None
    if last_ts is not None and last_util is not None:
        gap = now - last_ts
        delta = current_util_5h - last_util
        if _MIN_GAP_S <= gap <= _MAX_GAP_S and delta >= 0:
            # Normalise delta to a "per 5-min pass" rate regardless of actual gap
            per_pass = delta * (300.0 / gap)
            if ewma is None:
                ewma = per_pass
                samples = 1
            else:
                ewma = _EWMA_ALPHA * per_pass + (1 - _EWMA_ALPHA) * ewma
                samples = min(samples + 1, 99)
            new_h["burn_rate_5h_ewma"] = ewma
            new_h["burn_samples"] = samples

    _save_burn_history(new_h)

    ewma_final = new_h.get("burn_rate_5h_ewma")
    if ewma_final and ewma_final > 0 and new_h.get("burn_samples", 0) >= 2:
        remaining_pct = (1 - current_util_5h) * 100
        passes_left = remaining_pct / (ewma_final * 100)
        result = {
            "burn_rate_pct_per_pass": round(ewma_final * 100, 2),
            "burn_samples": new_h["burn_samples"],
            "estimated_passes_left": round(passes_left, 1),
            "estimated_minutes_left": round(passes_left * 5),
        }
    return result


def main():
    data = json.loads(QUOTA_FILE.read_text())
    headers = data.get("headers", {})

    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    util_5h = float(headers.get("anthropic-ratelimit-unified-5h-utilization", 0))
    util_7d = float(headers.get("anthropic-ratelimit-unified-7d-utilization", 0))
    reset_5h = headers.get("anthropic-ratelimit-unified-5h-reset", "")
    reset_7d = headers.get("anthropic-ratelimit-unified-7d-reset", "")

    result = {
        "status": status,
        "available": status == "allowed",
        "utilization_5h": util_5h,
        "utilization_7d": util_7d,
        "remaining_5h_pct": round((1 - util_5h) * 100),
        "remaining_7d_pct": round((1 - util_7d) * 100),
    }

    if reset_5h:
        result["reset_5h"] = datetime.fromtimestamp(int(reset_5h)).isoformat()
    if reset_7d:
        result["reset_7d"] = datetime.fromtimestamp(int(reset_7d)).isoformat()

    if "--gate" not in sys.argv:
        burn = _update_burn_rate(util_5h)
        if burn:
            result["burn"] = burn

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
    if result.get("burn"):
        b = result["burn"]
        print(f"Burn rate: {b['burn_rate_pct_per_pass']}%/pass ({b['burn_samples']} samples)")
        print(f"Est. passes left: {b['estimated_passes_left']} (~{b['estimated_minutes_left']}m)")


if __name__ == "__main__":
    main()
