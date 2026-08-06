#!/usr/bin/env python3
"""Read Codex weekly rate-limit telemetry and choose proactive-loop depth.

Codex CLI writes ``rate_limits`` snapshots into JSONL session transcripts.  A
single transcript can contain several limit lanes (for example, the normal
Codex lane and Spark), so the gate uses the most-utilized weekly lane as a
conservative bound.  Missing or entirely stale telemetry fails closed to
LIGHT.

The output is intentionally small and machine-readable with ``--json`` so the
proactive-loop skill can make a quota decision without pretending that the
Claude-only quota tracker is a Codex signal.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

STALE_AFTER_SECONDS = 30 * 60
LOOKBACK_SECONDS = 14 * 24 * 60 * 60
WEEKLY_MINUTES = 7 * 24 * 60  # the quota window this gate protects (10080 min)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "src"))
    try:
        from sutando_config import resolve_core_config_dirs

        for entry in resolve_core_config_dirs(repo):
            if entry.get("type") == "codex" and entry.get("value"):
                return Path(str(entry["value"])).expanduser()
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    return Path()


def _rate_limits(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    direct = payload.get("rate_limits")
    if isinstance(direct, dict):
        return direct
    info = payload.get("info")
    if isinstance(info, dict) and isinstance(info.get("rate_limits"), dict):
        return info["rate_limits"]
    return None


def _finite_number(value: Any) -> bool:
    """True only for a real finite int/float (not bool, not NaN/±inf). The cache
    is JSON parsed with json.loads(), which accepts NaN/Infinity tokens, so a
    telemetry field can be a non-finite float that passes an isinstance check but
    breaks int()/comparisons (qingyun CR #2676)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _weekly_window(limits: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The weekly rate-limit window from one ``rate_limits`` snapshot, or None.

    Codex does NOT always put the weekly quota in ``primary``: when a shorter
    (e.g. 300-minute) window is also being reported it lands in ``primary`` and
    the 10,080-minute weekly window is in ``secondary`` (qingyun CR #2676).  So
    select the weekly window by ``window_minutes`` (>= 7 days) across BOTH keys
    rather than assuming a fixed slot; assuming ``primary`` silently drops the
    whole snapshot and reports LIGHT even though usable weekly telemetry exists.
    When both windows qualify, take the longest (the true weekly)."""
    candidates: list[tuple[int, dict[str, Any]]] = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        # Require FINITE numerics: json.loads() accepts NaN/Infinity tokens by
        # default, and a NaN is a float that passes an isinstance check but then
        # poisons everything — int(NaN) raises ValueError (qingyun CR #2676), and
        # a NaN used_percent would sail through the used-clamp as NaN. A malformed
        # window must drop out so the gate fails closed to unavailable/LIGHT.
        if not _finite_number(window.get("used_percent")):
            continue
        window_minutes = window.get("window_minutes")
        if not _finite_number(window_minutes):
            continue
        if int(window_minutes) < WEEKLY_MINUTES:
            continue
        candidates.append((int(window_minutes), window))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]


def _snapshots(home: Path) -> list[tuple[float, dict[str, Any]]]:
    root = home / "sessions"
    if not root.exists():
        return []
    cutoff = time.time() - LOOKBACK_SECONDS
    found: list[tuple[float, dict[str, Any]]] = []
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    limits = _rate_limits(event.get("payload", event))
                    if not limits:
                        continue
                    raw_ts = event.get("timestamp")
                    try:
                        if isinstance(raw_ts, (int, float)):
                            stamp = float(raw_ts)
                        else:
                            stamp = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        stamp = path.stat().st_mtime
                    found.append((stamp, limits))
        except OSError:
            continue
    return found


def read_quota(home: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    latest: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
    for stamp, limits in _snapshots(home or _codex_home()):
        # The weekly window is the quota this gate protects — find it in either
        # `primary` or `secondary` by window length, don't assume the slot.
        weekly = _weekly_window(limits)
        if weekly is None:
            continue
        lane = str(limits.get("limit_id") or limits.get("limit_name") or "unknown")
        if lane not in latest or stamp >= latest[lane][0]:
            latest[lane] = (stamp, limits, weekly)

    rows = []
    for lane, (stamp, limits, weekly) in latest.items():
        used = max(0.0, min(100.0, float(weekly["used_percent"])))
        rows.append({
            "limit_id": lane,
            "limit_name": limits.get("limit_name"),
            "used_percent": round(used, 1),
            "remaining_percent": round(100.0 - used, 1),
            "resets_at": weekly.get("resets_at"),
            "sample_age_seconds": max(0, round(now - stamp)),
        })

    if not rows:
        return {"available": False, "tier": "LIGHT", "reason": "missing-or-stale", "limits": rows}

    # Keep stale lanes in the conservative bound when a fresh lane exists: a
    # lane can be quiet simply because the current turn used another model,
    # while its weekly limit is still real.  If every lane is stale, fail
    # closed to LIGHT instead of making a confident decision from old data.
    if all(row["sample_age_seconds"] > STALE_AFTER_SECONDS for row in rows):
        return {"available": False, "tier": "LIGHT", "reason": "missing-or-stale", "limits": rows}

    worst = min(rows, key=lambda row: row["remaining_percent"])
    remaining = worst["remaining_percent"]
    if remaining > 20:
        tier = "FULL"
    elif remaining >= 5:
        tier = "MEDIUM"
    else:
        tier = "LIGHT"
    return {
        "available": True,
        "tier": tier,
        "remaining_percent": remaining,
        "stale_lanes": [row["limit_id"] for row in rows if row["sample_age_seconds"] > STALE_AFTER_SECONDS],
        "limits": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = read_quota()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if not result["available"]:
            print("Codex quota: unavailable/stale; gate=LIGHT")
        else:
            print(f"Codex quota: {result['remaining_percent']}% remaining; gate={result['tier']}")
        for row in result.get("limits", []):
            print(f"  {row['limit_id']}: {row['remaining_percent']}% remaining ({row['sample_age_seconds']}s old)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
