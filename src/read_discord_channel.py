#!/usr/bin/env python3
"""Gated Discord channel reader — the ONLY sanctioned way for the agent to pull a
Discord channel's content into its context.

Why this exists (Susan 2026-06-17): "如果一个 channel 在 contextNotFrom，在 build
context 的时候 skip 这个 channel 的内容." The bridge already gates the `<#ref>`
prefetch (load_channel_context_blacklist in discord-bridge.py). But the agent can
ALSO read a channel by raw-curling the Discord API — that path bypassed the gate
and is exactly how a private channel (#pr-review, in the private guild) leaked
into a reply built for a public channel. There is no way to "un-see" private text
once it's in context, so the only reliable enforcement is at INGESTION: refuse to
fetch a blacklisted channel before its content ever enters context.

The decision is SERVING-RELATIVE (Susan's pr-review point: "如果 serve 的是 pr-review
你就不能拦截啊"). The blacklist is whatever the channel *being served* (the task's
origin) declares in `contextNotFrom` — NOT a global ban on the target:
  * serve #dev (contextNotFrom = [private-guild]) + read #pr-review  -> BLOCKED
  * serve #pr-review (its own contextNotFrom lacks itself) + read #pr-review -> ALLOWED

It's a plain blacklist (Susan: "黑名单就行了，不要搞那么复杂") — entries may be
CHANNEL ids or GUILD ids (a guild id blocks every channel in that guild), mirroring
discord-bridge.load_channel_context_blacklist exactly (same access.json, same shape).

Usage:
  python3 src/read_discord_channel.py --serving <origin_channel_id> --target <channel_id> [-n N]

  --serving  the channel the current task came FROM (its `channel_id`). Read it
             straight off the task file you are processing.
  --target   the channel you want to read.
  -n         how many recent messages (default 10).

Exit codes: 0 = content printed; 2 = BLOCKED by contextNotFrom (nothing fetched);
1 = operational error (no token / fetch failed). Fetches NOTHING on a block.
"""
import argparse, json, os, sys, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util_paths import claude_home_path  # canonical ~/.claude/ resolver (no hand-rolled paths)

ACCESS_FILE = claude_home_path("channels", "discord", "access.json")
ENV_FILE = claude_home_path("channels", "discord", ".env")
API = "https://discord.com/api/v10"


def load_channel_context_blacklist(serving_channel_id):
    """Set of ids (channel OR guild) the SERVING channel must not pull context
    from. Mirrors discord-bridge.load_channel_context_blacklist — one source of
    truth is the access.json file itself. Empty set if unconfigured."""
    try:
        data = json.loads(ACCESS_FILE.read_text())
        grp = data.get("groups", {}).get(str(serving_channel_id))
        if isinstance(grp, dict):
            return {str(c) for c in (grp.get("contextNotFrom") or [])}
    except Exception:
        pass
    return set()


def _bot_token():
    """Read DISCORD_BOT_TOKEN from the channel .env (never printed)."""
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _api_get(path, token):
    # Discord's edge (Cloudflare) 403s requests with urllib's default
    # "Python-urllib/x" User-Agent — a real UA is mandatory for the bot API.
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/sonichi/sutando, 1.0)",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_guild(target_channel_id, token):
    """Return the target channel's guild_id (str) or None. Isolated so the test
    can stub it without a live Discord."""
    try:
        ch = _api_get(f"/channels/{target_channel_id}", token)
        gid = ch.get("guild_id")
        return str(gid) if gid is not None else None
    except Exception as e:
        print(f"[read-discord-channel] guild resolve failed for {target_channel_id}: {e}", file=sys.stderr)
        return None


def fetch_messages(target_channel_id, n, token):
    """Return a printable string of the N most recent messages. Isolated so the
    test can stub it."""
    msgs = _api_get(f"/channels/{target_channel_id}/messages?limit={int(n)}", token)
    out = []
    for m in reversed(msgs):  # oldest-first reads naturally
        author = (m.get("author") or {}).get("username", "?")
        content = m.get("content", "")
        out.append(f"[{author}] {content}")
    return "\n".join(out) if out else "(no messages)"


def gate(serving_channel_id, target_channel_id, token):
    """Return None if reading target is ALLOWED for this serving channel, else a
    block-reason string. Pure decision (given a guild resolver)."""
    blacklist = load_channel_context_blacklist(serving_channel_id)
    if not blacklist:
        return None
    if str(target_channel_id) in blacklist:
        return (f"#{target_channel_id} is in the contextNotFrom of the serving "
                f"channel {serving_channel_id} (channel-level entry)")
    guild = resolve_guild(target_channel_id, token)
    if guild is None:
        # FAIL-CLOSED: the serving channel HAS a blacklist but we cannot verify
        # which guild the target belongs to (resolve failed / no access). A
        # privacy gate must not fetch what it cannot clear — if the target were
        # in a blacklisted guild, fetching would leak. Refuse.
        return (f"could not verify the guild of #{target_channel_id}; the serving "
                f"channel {serving_channel_id} has a contextNotFrom blacklist, so "
                f"refusing rather than risk reading a blacklisted guild (fail-closed)")
    if guild in blacklist:
        return (f"#{target_channel_id} is in guild {guild}, which is in the "
                f"contextNotFrom of the serving channel {serving_channel_id} (guild-level entry)")
    return None


def main():
    ap = argparse.ArgumentParser(description="Gated Discord channel reader (contextNotFrom-aware).")
    ap.add_argument("--serving", required=True, help="origin channel_id of the task being served")
    ap.add_argument("--target", required=True, help="channel_id to read")
    ap.add_argument("-n", type=int, default=10, help="recent messages to fetch (default 10)")
    args = ap.parse_args()

    token = _bot_token()
    if not token:
        print("[read-discord-channel] no DISCORD_BOT_TOKEN available", file=sys.stderr)
        return 1

    reason = gate(args.serving, args.target, token)
    if reason is not None:
        print(f"BLOCKED: {reason}. Refusing to add its content to context "
              f"(Susan's contextNotFrom rule). Nothing was fetched.")
        return 2

    try:
        print(fetch_messages(args.target, args.n, token))
    except Exception as e:
        print(f"[read-discord-channel] fetch failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
