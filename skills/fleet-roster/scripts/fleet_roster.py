"""Fleet roster — canonical name→Discord ID lookup for Sutando agents.

Source of truth: FLEET_ROSTER_PATH env var, or workspace/data/fleet-roster.json
(path is configurable to survive workspace-revamp migration).

Static name→ID lookup only. Live channel-membership queries ("is X in channel Y?")
require the discord-bridge list_channel_members() function.

Per Air's review (2026-06-06): the data file home is intentionally deferred until
the workspace revamp (#1454, #1449) lands — path is read from env var so it migrates
without code changes.

Usage:
    from fleet_roster import mention, get_member

    mention("pro")           # → "<@1509329143110565888>"
    get_member("mini")       # → {"id": "...", "channels": [...], "role": "agent", "guild": "..."}
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Default roster — updated by data/fleet-roster.json when available.
# IDs confirmed by Mini on 2026-06-06 (Echo Act IV fleet).
_BUILTIN_ROSTER: dict[str, dict] = {
    "air":   {"id": "1485364006297534584", "role": "agent", "name": "sutando"},
    "mini":  {"id": "1490412828065267872", "role": "agent", "name": "Sutando-Mini"},
    "pro":   {"id": "1509329143110565888", "role": "agent", "name": "Echo Act IV Pro"},
    "lucy":  {"id": "1494435872949665953", "role": "agent", "name": "Lucy-Studio-Susan"},
}

# Path is configurable via FLEET_ROSTER_PATH env var so it survives workspace migration.
# Default falls back to workspace/data/ but can be overridden without code changes.
_ROSTER_PATH = Path(
    os.environ.get(
        "FLEET_ROSTER_PATH",
        str(
            Path(os.environ.get("SUTANDO_WORKSPACE", str(Path.home() / ".sutando" / "workspace")))
            / "data" / "fleet-roster.json"
        )
    )
)


def _load_roster() -> dict[str, dict]:
    """Load roster from data/fleet-roster.json, falling back to builtin."""
    try:
        if _ROSTER_PATH.exists():
            data = json.loads(_ROSTER_PATH.read_text())
            if isinstance(data, dict):
                return {k.lower(): v for k, v in data.items()}
    except Exception:
        pass
    return _BUILTIN_ROSTER


def get_member(name: str) -> Optional[dict]:
    """Return member record for canonical name, or None if not found."""
    roster = _load_roster()
    return roster.get(name.lower())


def mention(name: str, platform: str = "discord") -> str:
    """Return platform-specific @-mention string for canonical name.

    platform: "discord" (default) or "ag2.space" (future cutover).
    Raises ValueError if name not in roster — fail loudly rather than
    sending a broken mention silently.
    """
    member = get_member(name)
    if member is None:
        raise ValueError(
            f"Unknown fleet member '{name}'. "
            f"Known: {list(_load_roster().keys())}. "
            f"Update {_ROSTER_PATH} to add new members."
        )
    if platform == "discord":
        return f"<@{member['id']}>"
    elif platform == "ag2.space":
        # ag2.space mention format TBD — use canonical name as placeholder
        return f"@{name}"
    else:
        raise ValueError(f"Unknown platform '{platform}'. Use 'discord' or 'ag2.space'.")


async def mention_verified(name: str, channel_id: int, platform: str = "discord") -> str:
    """Return mention string, verified against live channel membership at call time.

    Calls discord-bridge list_channel_members() to confirm the member is actually
    in the target channel before returning the mention. This is the load-bearing
    check that prevents posting mentions to channels where the target bot can't see.

    IMPORTANT: requires discord-bridge client to be running and GUILD_MEMBERS intent
    enabled. If list_channel_members() returns empty (intent missing or bot not in
    guild), raises ValueError to avoid false-negative refusals. Caller must handle
    the guild-intent bootstrap separately.

    Dependency direction: skill → discord-bridge. Never call this from core.
    """
    # Import at call time to avoid circular import; discord-bridge is a peer
    import importlib.util
    import sys
    bridge_path = Path(__file__).parents[3] / "src" / "discord-bridge.py"
    if not bridge_path.exists():
        raise RuntimeError(
            f"discord-bridge.py not found at {bridge_path}. "
            "mention_verified() requires the discord-bridge to be present."
        )

    # Call list_channel_members from the running bridge via direct import
    spec = importlib.util.spec_from_file_location("discord_bridge", bridge_path)
    if spec is None:
        raise RuntimeError("Could not load discord-bridge spec")
    bridge = importlib.util.module_from_spec(spec)

    member = get_member(name)
    if member is None:
        raise ValueError(f"Unknown fleet member '{name}'.")

    try:
        spec.loader.exec_module(bridge)  # type: ignore
        members = await bridge.list_channel_members(channel_id)
    except Exception as e:
        # If the query fails, fall back to unverified mention with a warning
        # rather than refusing (trades refusal bug for drift bug — choose drift)
        import warnings
        warnings.warn(
            f"list_channel_members failed ({e}); falling back to unverified mention. "
            "Verify GUILD_MEMBERS intent is enabled."
        )
        return mention(name, platform)

    if not members:
        # Empty result = intent missing or no members visible.
        # Fall back rather than false-negative.
        import warnings
        warnings.warn(
            f"list_channel_members returned empty for channel {channel_id}. "
            "GUILD_MEMBERS intent may not be enabled. Using unverified mention."
        )
        return mention(name, platform)

    member_ids = {m["id"] for m in members}
    if member["id"] not in member_ids:
        raise ValueError(
            f"Fleet member '{name}' (id {member['id']}) is not in channel {channel_id}. "
            "Cannot post a mention they cannot see."
        )

    return mention(name, platform)


def list_members() -> list[dict]:
    """Return all known fleet members with their metadata."""
    roster = _load_roster()
    return [{"name": k, **v} for k, v in roster.items()]
