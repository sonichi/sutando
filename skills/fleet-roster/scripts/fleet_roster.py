"""Fleet roster — canonical name→Discord ID resolution for Sutando agents.

## Design

The **code** (this file) ships with ZERO IDs. It is safe to share, review,
and commit to a public repo because it contains only resolution logic.

The **data** (name→ID map) lives in a private per-host file, never committed:
  ~/.claude/fleet-roster.local.json

Each fleet host installs its own roster file. The bot's own Discord ID is
derived live via /users/@me and never written to disk.

## Privacy invariant

IDs never appear in any committed source or memory-synced file.
  - fleet_roster.py: NO IDs
  - fleet-roster.local.json: per-host, gitignored, outside memory-sync
  - access.json: raw numbers, no names (already private)

## Usage

    from fleet_roster import mention, get_member

    mention("pro")                          # → "<@id>" (reads private roster)
    mention("pro", platform="ag2.space")    # → "@pro" (future cutover)

    # Channel-verified mention (live Discord query at post-time):
    import asyncio
    asyncio.run(mention_verified("pro", channel_id=CHANNEL_ID))
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Private per-host roster file — NOT committed, NOT memory-synced.
# Default: $SUTANDO_MEMORY_DIR/fleet-roster.local.json (resolves at runtime
# so it follows the Memory-space path after the workspace-revamp migration).
# Override via FLEET_ROSTER_PATH env var.
#
# The data file is DEFERRED until the Memory-space migration settles (#1449,
# #1454, #1490). Ship the code now; install the file after migration lands.
_MEMORY_DIR = os.environ.get(
    "SUTANDO_MEMORY_DIR",
    str(Path.home() / ".claude" / "projects")  # last-resort fallback only
)
_ROSTER_PATH = Path(
    os.environ.get(
        "FLEET_ROSTER_PATH",
        str(Path(_MEMORY_DIR) / "fleet-roster.local.json")
    )
)


def _load_roster() -> dict[str, dict]:
    """Load roster from private local file. Raises FileNotFoundError if missing."""
    if not _ROSTER_PATH.exists():
        raise FileNotFoundError(
            f"Fleet roster not found at {_ROSTER_PATH}. "
            "Create it with your fleet's name→ID map. "
            "See skills/fleet-roster/SKILL.md for format. "
            "This file is private — never commit or memory-sync it."
        )
    try:
        data = json.loads(_ROSTER_PATH.read_text())
        if not isinstance(data, dict):
            raise ValueError("roster must be a JSON object")
        return {k.lower(): v for k, v in data.items()}
    except Exception as e:
        raise RuntimeError(f"Failed to load fleet roster from {_ROSTER_PATH}: {e}")


def get_member(name: str) -> Optional[dict]:
    """Return member record for canonical name, or None if not found."""
    try:
        roster = _load_roster()
    except FileNotFoundError:
        return None
    return roster.get(name.lower())


def mention(name: str, platform: str = "discord") -> str:
    """Return platform-specific @-mention string for canonical name.

    platform: "discord" (default) or "ag2.space" (future cutover).
    Raises FileNotFoundError if roster not installed.
    Raises ValueError if name not in roster.
    """
    roster = _load_roster()
    member = roster.get(name.lower())
    if member is None:
        raise ValueError(
            f"Unknown fleet member '{name}'. "
            f"Known: {list(roster.keys())}. "
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

    Calls discord-bridge list_channel_members() to confirm the member is
    in the target channel before returning the mention. This prevents posting
    mentions to channels where the target bot cannot see.

    Dependency direction: skill → discord-bridge, never reverse.

    Falls back to unverified mention (with warning) if:
    - GUILD_MEMBERS intent unavailable (empty member list)
    - Bridge unavailable
    This avoids trading the drift bug for a refusal bug.
    """
    import importlib.util
    import warnings

    bridge_path = Path(__file__).parents[3] / "src" / "discord-bridge.py"
    if not bridge_path.exists():
        warnings.warn(f"discord-bridge.py not found at {bridge_path}; using unverified mention")
        return mention(name, platform)

    member = _load_roster().get(name.lower())
    if member is None:
        raise ValueError(f"Unknown fleet member '{name}'.")

    try:
        spec = importlib.util.spec_from_file_location("discord_bridge", bridge_path)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)  # type: ignore
        members = await bridge.list_channel_members(channel_id)
    except Exception as e:
        warnings.warn(f"list_channel_members failed ({e}); using unverified mention")
        return mention(name, platform)

    if not members:
        warnings.warn(
            f"list_channel_members returned empty for channel {channel_id}; "
            "GUILD_MEMBERS intent may not be enabled. Using unverified mention."
        )
        return mention(name, platform)

    member_ids = {m["id"] for m in members}
    if member["id"] not in member_ids:
        raise ValueError(
            f"Fleet member '{name}' (id {member['id']}) is not in channel {channel_id}."
        )

    return mention(name, platform)


def list_members() -> list[dict]:
    """Return all known fleet members with their metadata."""
    roster = _load_roster()
    return [{"name": k, **v} for k, v in roster.items()]
