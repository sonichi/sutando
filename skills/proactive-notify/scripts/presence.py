"""Read owner-presence signals used by the channel router.

Reads existing state files written by other Sutando services:
- state/last-owner-activity.json (bridges)
- state/voice-session-context.json (voice-agent)
- state/presenter-mode.sentinel (scripts/presenter-mode.sh)
- logs/voice-agent.log tail (voice client connected line)

Returns a PresenceSnapshot with named boolean predicates the policy
yaml's `if:` clauses reference by name.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
STATE_DIR = WORKSPACE / "state"
VOICE_LOG = WORKSPACE / "logs" / "voice-agent.log"


@dataclass
class PresenceSnapshot:
    presenter_mode_active: bool
    voice_client_connected: bool
    owner_active_in_discord_within_min_5: bool
    in_quiet_hours: bool


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _presenter_mode_active() -> bool:
    sentinel = STATE_DIR / "presenter-mode.sentinel"
    if not sentinel.exists():
        return False
    try:
        expire_iso = sentinel.read_text().strip()
        if not expire_iso or not expire_iso[0].isdigit():
            return False
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return now_iso < expire_iso
    except Exception:
        return False


def _voice_client_connected() -> bool:
    if not VOICE_LOG.exists():
        return False
    try:
        with VOICE_LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if "[Health]" in line and "client=" in line:
                return "client=true" in line
    except Exception:
        pass
    return False


def _owner_active_in_discord_within(minutes: int) -> bool:
    activity = _read_json(STATE_DIR / "last-owner-activity.json")
    ts = activity.get("ts")
    channel = activity.get("channel")
    if not isinstance(ts, (int, float)) or channel != "discord":
        return False
    return (time.time() - ts) < minutes * 60


def _in_quiet_hours(policy: dict) -> bool:
    quiet = policy.get("quiet_hours") or {}
    start = quiet.get("start")
    end = quiet.get("end")
    tz = quiet.get("timezone", "America/Los_Angeles")
    if not start or not end:
        return False
    try:
        zone = ZoneInfo(tz)
        from datetime import datetime
        now = datetime.now(zone).time().strftime("%H:%M")
    except Exception:
        from datetime import datetime
        now = datetime.now().strftime("%H:%M")
    # Handle wrap-around (e.g., 23:00 -> 07:00).
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def snapshot(policy: dict) -> PresenceSnapshot:
    return PresenceSnapshot(
        presenter_mode_active=_presenter_mode_active(),
        voice_client_connected=_voice_client_connected(),
        owner_active_in_discord_within_min_5=_owner_active_in_discord_within(5),
        in_quiet_hours=_in_quiet_hours(policy),
    )
