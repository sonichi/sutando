#!/usr/bin/env python3
"""Choose the Claude proactive-loop cadence from fresh 7-day quota state."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

SEVEN_DAY_THRESHOLD = 0.80
THROTTLED_MINUTES = 30
STALE_AFTER_SECONDS = 30 * 60
FALLBACK_NORMAL_CRON = "*/10 * * * *"
_INTERVAL_CRON = re.compile(r"^\*/([1-9][0-9]*) \* \* \* \*$")


def _finite_fraction(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        return None
    return parsed


def _normal_cron(crons: Any) -> str:
    if not isinstance(crons, list):
        return FALLBACK_NORMAL_CRON
    for entry in crons:
        if not isinstance(entry, dict):
            continue
        prompt = entry.get("prompt")
        if (entry.get("name") == "main-loop"
                or entry.get("prompt_skill") == "proactive-loop"
                or (isinstance(prompt, str) and prompt.strip() == "/proactive-loop")):
            cron = entry.get("cron")
            if isinstance(cron, str) and cron.strip():
                return cron.strip()
    return FALLBACK_NORMAL_CRON


def _utilization_7d(quota: Any) -> Optional[float]:
    if not isinstance(quota, dict):
        return None
    direct = _finite_fraction(quota.get("utilization_7d"))
    if direct is not None:
        return direct
    headers = quota.get("headers")
    if not isinstance(headers, dict):
        return None
    return _finite_fraction(
        headers.get("anthropic-ratelimit-unified-7d-utilization")
    )


def choose_cadence(
    quota: Any,
    crons: Any,
    *,
    quota_age_seconds: Optional[float],
    base_url: Optional[str],
) -> dict[str, Any]:
    normal_cron = _normal_cron(crons)
    match = _INTERVAL_CRON.fullmatch(normal_cron)
    utilization = _utilization_7d(quota)
    stale = quota_age_seconds is None or quota_age_seconds > STALE_AFTER_SECONDS
    repo = Path(__file__).resolve().parents[3]
    quota_scripts = repo / "skills" / "quota-tracker" / "scripts"
    sys.path.insert(0, str(quota_scripts))
    from quota_availability import availability_decision

    decision = availability_decision(quota, base_url=base_url, stale=stale)
    available = decision["available"] and utilization is not None
    over_threshold = available and utilization >= SEVEN_DAY_THRESHOLD
    fail_safe = not available

    if not match:
        return {
            "available": available,
            "effective_cron": normal_cron,
            "normal_cron": normal_cron,
            "reason": "unsupported-normal-cron",
            "seven_day_threshold": SEVEN_DAY_THRESHOLD,
            "throttled": False,
            "utilization_7d": utilization,
            "unavailable_reason": decision["unavailable_reason"],
        }

    normal_minutes = int(match.group(1))
    should_throttle = (over_threshold or fail_safe) and normal_minutes < THROTTLED_MINUTES
    return {
        "available": available,
        "effective_cron": (
            f"*/{THROTTLED_MINUTES} * * * *" if should_throttle else normal_cron
        ),
        "normal_cron": normal_cron,
        "reason": (
            "7d-threshold" if over_threshold
            else "quota-unavailable" if fail_safe
            else "below-threshold"
        ),
        "seven_day_threshold": SEVEN_DAY_THRESHOLD,
        "throttled": should_throttle,
        "utilization_7d": utilization,
        "unavailable_reason": decision["unavailable_reason"],
    }


def evaluate_paths(
    quota_path: Path,
    crons_path: Path,
    *,
    now: Optional[float] = None,
    base_url: Optional[str] = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    try:
        quota = json.loads(quota_path.read_text())
        quota_age = max(0.0, now - quota_path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        quota = None
        quota_age = None
    try:
        crons = json.loads(crons_path.read_text())
    except (OSError, TypeError, ValueError):
        crons = None
    return choose_cadence(
        quota,
        crons,
        quota_age_seconds=quota_age,
        base_url=base_url,
    )


def _runtime_paths() -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "src"))
    from util_paths import _host_label
    from workspace_default import resolve_workspace, status_read_path

    workspace = resolve_workspace(migrate=False)
    return (
        status_read_path("quota-state.json", workspace),
        workspace / "hosts" / _host_label() / "crons.json",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    quota_path, crons_path = _runtime_paths()
    result = evaluate_paths(
        quota_path,
        crons_path,
        base_url=os.environ.get("ANTHROPIC_BASE_URL"),
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Claude proactive cadence: {result['effective_cron']} "
            f"({result['reason']}, 7d={result['utilization_7d']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
