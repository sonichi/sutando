#!/usr/bin/env python3
"""Post a coordination message from this bot to the #bot2bot channel.

Usage:
    python3 skills/bot2bot-post/post.py <kind> <text>
    python3 skills/bot2bot-post/post.py claim "refactor X, ETA 20m"
    python3 skills/bot2bot-post/post.py blocked "Twilio credentials expired"
    python3 skills/bot2bot-post/post.py done "shipped PR #472"
    python3 skills/bot2bot-post/post.py ping "need your take on the IV halo"

Kinds: claim | blocked | done | ping | opinion

The target channel ID is read from `~/.claude/channels/discord/access.json`:
entries tagged with `{"role": "bot2bot", ...}` in `groups` are candidates. We
pick the first such channel. If none is tagged, we fall back to the first
group whose value is just `true` (legacy convention), or error out.

The other bot's user ID is read from the same file's `allowFrom` list,
excluding this bot (identified via Discord GET /users/@me). The resulting
`<@id>` mention is prepended so the receiving bot's bridge will process it
as a task (discord-bridge.py line 244 exception).

Requires DISCORD_BOT_TOKEN in ~/.claude/channels/discord/.env.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

ACCESS_JSON = Path.home() / ".claude" / "channels" / "discord" / "access.json"
ENV_FILE = Path.home() / ".claude" / "channels" / "discord" / ".env"
VALID_KINDS = {"claim", "blocked", "done", "ping", "opinion"}


def load_token() -> str:
    """Load DISCORD_BOT_TOKEN from the Discord channel's .env."""
    if not ENV_FILE.exists():
        sys.exit(f"ERROR: {ENV_FILE} not found")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("ERROR: DISCORD_BOT_TOKEN not in env")


def load_access() -> dict:
    if not ACCESS_JSON.exists():
        sys.exit(f"ERROR: {ACCESS_JSON} not found")
    return json.loads(ACCESS_JSON.read_text())


def resolve_bot2bot_channel(access: dict) -> str:
    """Pick the bot2bot channel from access.json.

    Preferred: groups entries with `{"role": "bot2bot", ...}`.
    Fallback: groups entries whose value is literal `true` (legacy).
    """
    groups = access.get("groups", {})
    # Preferred: explicitly tagged
    for cid, cfg in groups.items():
        if isinstance(cfg, dict) and cfg.get("role") == "bot2bot":
            return cid
    # Fallback: first `true`-valued group (legacy — likely the bot2bot one)
    for cid, cfg in groups.items():
        if cfg is True:
            return cid
    sys.exit("ERROR: no bot2bot channel found in access.json.groups")


USER_AGENT = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"


def get_self_id(token: str) -> str:
    """Discord GET /users/@me → this bot's user ID."""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["id"]


def resolve_other_bot(access: dict, self_id: str, channel_id: str):
    """Find the other bot's user ID from the bot2bot CHANNEL's allowFrom.

    The top-level `allowFrom` is owner-only by the tier-isolation invariant
    (see `scripts/validate-access-tiers.py`) — sibling bots must not appear
    there or they'd be classified as access_tier=owner instead of team.
    The sibling-bot ID lives in the #bot2bot channel's allowFrom.

    Falls back to the top-level allowFrom for older configs that haven't
    migrated to channel-level allowFrom yet.
    """
    ch_cfg = access.get("groups", {}).get(channel_id)
    allow: list = []
    if isinstance(ch_cfg, dict):
        allow = list(ch_cfg.get("allowFrom", []))
    # Fallback: legacy configs that only have top-level allowFrom
    if not allow:
        allow = list(access.get("allowFrom", []))
    others = [uid for uid in allow if uid != self_id]
    if not others:
        return None
    # Heuristic: the sibling-bot ID will not match self_id. The owner's
    # user_id may also appear in the channel allowFrom; to pick the bot,
    # prefer the ID that is NOT in the top-level allowFrom (owner-only).
    global_allow = set(str(x) for x in access.get("allowFrom", []))
    bot_candidates = [uid for uid in others if str(uid) not in global_allow]
    if bot_candidates:
        return bot_candidates[0]
    # Last resort: any non-self ID (legacy configs where owner+bot share the
    # top-level allowFrom).
    return others[0]


def post(channel_id: str, text: str, token: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: Discord API {e.code}: {e.read().decode()}")


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    kind = sys.argv[1]
    text = " ".join(sys.argv[2:])
    if kind not in VALID_KINDS:
        sys.exit(f"ERROR: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    token = load_token()
    access = load_access()
    channel_id = resolve_bot2bot_channel(access)
    self_id = get_self_id(token)
    other_id = resolve_other_bot(access, self_id, channel_id)

    prefix = f"<@{other_id}> " if other_id else ""
    message = f"{prefix}{kind}: {text}"

    result = post(channel_id, message, token)
    print(f"Posted to #{channel_id} (msg_id {result.get('id')}): {message[:80]}")


if __name__ == "__main__":
    main()
