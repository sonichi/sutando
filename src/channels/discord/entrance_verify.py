#!/usr/bin/env python3
"""Discord entrance verification — the provider-I/O edge of EntranceLink.

Token introspection (GET /users/@me) runs HERE, through the shared
DiscordRestClient chokepoint; the domain owner (src/entrance_links.py) only
records the verified facts and never touches a provider API. Credential
material never leaves this function — the record carries a fingerprint only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import entrance_links
from channels.discord.client import DiscordRestClient


def verify_discord(state_dir: "str | Path", token: str,
                   client: "DiscordRestClient | None" = None) -> dict:
    """Introspect the bot token at the Discord edge and record the verified
    subject as an EntranceLink. `client` is injectable for tests."""
    me = (client or DiscordRestClient(token)).get_json("/users/@me")
    subject = {"type": "bot_user", "id": str(me["id"])}
    display = {}
    name = me.get("global_name") or me.get("username")
    if name:
        display["name"] = name
    if me.get("avatar"):
        display["avatar_url"] = (f"https://cdn.discordapp.com/avatars/"
                                 f"{me['id']}/{me['avatar']}.png")
    verification = {
        "method": "discord_token_introspection",
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return entrance_links.upsert_link(
        state_dir, "discord", subject, verification,
        entrance_links.credential_fingerprint(token), display=display or None)
