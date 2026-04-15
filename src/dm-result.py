#!/usr/bin/env python3
"""Send a task result to Discord DM if voice client is disconnected.

Usage:
    python3 src/dm-result.py "Result text here"
    python3 src/dm-result.py --file results/task-123.txt

Checks http://localhost:8080/sse-status for voiceConnected.
If voice is connected, does nothing (voice agent will speak the result).
If voice is disconnected, sends the result to the owner's Discord DM.

Requires DISCORD_BOT_TOKEN in .env (or in ~/.claude/channels/discord/.env)
and the Discord bridge running.

Owner resolution:
    1. $SUTANDO_DM_OWNER_ID env var (explicit override).
    2. First non-bot user in ~/.claude/channels/discord/access.json → allowFrom.
The bot's own user ID is discovered via Discord's GET /users/@me so that
multi-owner allowFrom lists still resolve to the human.

Per-node correctness:
    The DM channel ID is NOT hardcoded — each node creates/opens its own
    DM channel on demand via POST /users/@me/channels (idempotent per
    Discord docs). This fixes the HTTP 403 seen on Mac Mini when the old
    hardcoded channel ID belonged to MacBook's bot's DM with the owner.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ACCESS_JSON = Path.home() / ".claude" / "channels" / "discord" / "access.json"
SSE_STATUS_URL = "http://localhost:8080/sse-status"


def voice_connected() -> bool:
    """Check if a voice client is currently connected."""
    try:
        with urllib.request.urlopen(SSE_STATUS_URL, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("voiceConnected", False)
    except Exception:
        return False


def _load_token() -> str:
    """Read DISCORD_BOT_TOKEN from the first env file that has it."""
    for env_path in [
        Path.home() / ".claude" / "channels" / "discord" / ".env",
        REPO / ".env",
    ]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _discord_api(method, path, token, body=None):
    """Small wrapper around urllib for Discord's REST API. Returns parsed JSON
    on 2xx, raises on other statuses. No retries — caller handles failure."""
    url = f"https://discord.com/api/v10{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Sutando/1.0",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _resolve_owner_id(token):
    """Return the Discord user ID for the human owner.

    Priority order:
      1. $SUTANDO_DM_OWNER_ID env var.
      2. First non-bot ID in ~/.claude/channels/discord/access.json →
         allowFrom. "Non-bot" is decided by querying GET /users/{id} and
         reading the `bot` field — allowFrom often contains multiple bots
         (MacBook bot, Mac Mini bot) plus the human owner, and we only
         want to DM the human.

    Set SUTANDO_DM_OWNER_ID in .env if you want to skip the per-id
    /users lookup (saves 1 API call per dm-result invocation)."""
    env_override = os.environ.get("SUTANDO_DM_OWNER_ID", "").strip()
    if env_override:
        return env_override

    if not ACCESS_JSON.exists():
        return ""
    try:
        data = json.loads(ACCESS_JSON.read_text())
    except Exception:
        return ""
    allow = data.get("allowFrom") or []
    if not allow:
        return ""

    # Query each user's is-bot flag. The first human wins. If lookups all
    # fail (rate limit, network, bad token), fall through to allow[0] as
    # a degraded default so send_dm() can produce an honest error later.
    for uid in allow:
        try:
            user = _discord_api("GET", f"/users/{uid}", token)
            if isinstance(user, dict) and not user.get("bot", False):
                return str(uid)
        except Exception:
            continue
    return str(allow[0])


def _open_dm_channel(owner_id: str, token: str) -> str:
    """Create/open a DM channel between this bot and owner_id. Returns the
    channel ID. Per Discord docs this endpoint is idempotent: if a DM already
    exists between the bot and the user, it returns that channel rather than
    creating a new one, so repeated calls are cheap."""
    resp = _discord_api("POST", "/users/@me/channels", token, {"recipient_id": owner_id})
    if isinstance(resp, dict) and "id" in resp:
        return str(resp["id"])
    raise RuntimeError(f"unexpected /users/@me/channels response: {resp!r}")


def send_dm(text: str) -> bool:
    """Send text to the resolved owner's Discord DM."""
    token = _load_token()
    if not token:
        print("dm-result: DISCORD_BOT_TOKEN not found in .env", file=sys.stderr)
        return False

    owner_id = _resolve_owner_id(token)
    if not owner_id:
        print("dm-result: could not resolve owner user ID (set SUTANDO_DM_OWNER_ID or populate access.json allowFrom)", file=sys.stderr)
        return False

    try:
        channel_id = _open_dm_channel(owner_id, token)
    except Exception as e:
        print(f"dm-result: failed to open DM channel with {owner_id}: {e}", file=sys.stderr)
        return False

    # Truncate if too long for Discord (2000 char limit; leave room for suffix).
    if len(text) > 1900:
        text = text[:1900] + "\n... (truncated)"

    try:
        _discord_api("POST", f"/channels/{channel_id}/messages", token, {"content": text})
    except Exception as e:
        print(f"dm-result: failed to send DM to channel {channel_id}: {e}", file=sys.stderr)
        return False

    print(f"dm-result: sent to DM ({len(text)} chars) via channel {channel_id}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 src/dm-result.py 'text' | --file path", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Usage: python3 src/dm-result.py --file path", file=sys.stderr)
            sys.exit(1)
        text = Path(sys.argv[2]).read_text().strip()
    else:
        text = " ".join(sys.argv[1:])

    if voice_connected():
        print("dm-result: voice client connected, skipping DM (voice will deliver)")
        return

    print("dm-result: voice client disconnected, sending to Discord DM")
    if send_dm(text):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
