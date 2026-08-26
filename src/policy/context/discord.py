#!/usr/bin/env python3
"""contextNotFrom gate — the single policy deciding whether a serving channel
may pull another Discord channel's content into context.

Why (Susan 2026-06-17): the bridge gates the `<#ref>` prefetch, but the agent
can also read a channel via the raw API — that path bypassed the gate and is
exactly how a private channel leaked into a reply built for a public channel.
There is no way to "un-see" private text once it's in context, so enforcement
lives at INGESTION: refuse to fetch a blacklisted channel before its content
ever enters context.

The decision is SERVING-RELATIVE: the blacklist is whatever the channel being
served (the task's origin) declares in `contextNotFrom` — not a global ban on
the target. Entries may be CHANNEL ids or GUILD ids (a guild id blocks every
channel in that guild), mirroring discord-bridge.load_channel_context_blacklist
(same access.json, same shape).

Both reader CLIs consume this module; `gate()` takes an injectable
guild resolver so callers (and their tests) can substitute resolution without
patching this module.
"""
from __future__ import annotations

import json
import sys
import urllib.request

from util_paths import claude_home_path
from channels.discord.http import request_json

ACCESS_FILE = claude_home_path("channels", "discord", "access.json")
API = "https://discord.com/api/v10"


def load_channel_context_blacklist(serving_channel_id, access_file=None):
    """Set of ids (channel OR guild) the SERVING channel must not pull context
    from. Mirrors discord-bridge.load_channel_context_blacklist — one source of
    truth is the access.json file itself. Empty set if unconfigured."""
    try:
        data = json.loads((access_file or ACCESS_FILE).read_text())
        grp = data.get("groups", {}).get(str(serving_channel_id))
        if isinstance(grp, dict):
            return {str(c) for c in (grp.get("contextNotFrom") or [])}
    except Exception:
        pass
    return set()


def resolve_guild(target_channel_id, token):
    """Return the target channel's guild_id (str) or None. Isolated so tests
    can stub it without a live Discord."""
    try:
        # A real UA is mandatory — Discord's edge 403s urllib's default.
        req = urllib.request.Request(f"{API}/channels/{target_channel_id}", headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/sonichi/sutando, 1.0)",
        })
        ch = request_json(req, timeout=10)
        gid = ch.get("guild_id")
        return str(gid) if gid is not None else None
    except Exception as e:
        print(f"[discord-context-policy] guild resolve failed for {target_channel_id}: {e}",
              file=sys.stderr)
        return None


def gate(serving_channel_id, target_channel_id, token, guild_resolver=None,
         access_file=None):
    """Return None if reading target is ALLOWED for this serving channel, else a
    block-reason string. Pure decision given a guild resolver."""
    resolver = guild_resolver if guild_resolver is not None else resolve_guild
    blacklist = load_channel_context_blacklist(serving_channel_id, access_file=access_file)
    if not blacklist:
        return None
    if str(target_channel_id) in blacklist:
        return (f"#{target_channel_id} is in the contextNotFrom of the serving "
                f"channel {serving_channel_id} (channel-level entry)")
    guild = resolver(target_channel_id, token)
    if guild is None:
        # FAIL-CLOSED: a privacy gate must not fetch what it cannot clear.
        return (f"could not verify the guild of #{target_channel_id}; the serving "
                f"channel {serving_channel_id} has a contextNotFrom blacklist, so "
                f"refusing rather than risk reading a blacklisted guild (fail-closed)")
    if guild in blacklist:
        return (f"#{target_channel_id} is in guild {guild}, which is in the "
                f"contextNotFrom of the serving channel {serving_channel_id} (guild-level entry)")
    return None
