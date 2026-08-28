#!/usr/bin/env python3
"""Gated Discord channel reader — compatibility wrapper over the shared reader
and the shared contextNotFrom policy.

The gate semantics (Susan 2026-06-17) and the CLI contract are unchanged; the
policy now lives in `policy/context/discord.py` (single implementation, shared
with discord-read.py's --serving mode) and the message rendering in
`channels/discord/reader.py`. The rendering upgrade is deliberate: this reader's private
copy printed `m.get("content")` only, so a forwarded message rendered BLANK —
the shared renderer labels forwards and reply context like the main reader.

Usage:
  python3 src/read_discord_channel.py --serving <origin_channel_id> --target <channel_id> [-n N]

Exit codes: 0 = content printed; 2 = BLOCKED by contextNotFrom (nothing fetched);
1 = operational error (no token / fetch failed). Fetches NOTHING on a block.
"""
# ruff: noqa: E402 — imports below require the sys.path insert above them
import argparse
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util_paths import claude_home_path
from channels.discord.http import request_json
import policy.context.discord as _policy
import channels.discord.reader as _reader
from channels.discord.reader import _redact  # noqa: F401  — shared policy, re-exported

ACCESS_FILE = _policy.ACCESS_FILE
ENV_FILE = claude_home_path("channels", "discord", ".env")
API = "https://discord.com/api/v10"

def load_channel_context_blacklist(serving_channel_id):
    # ACCESS_FILE resolves through THIS module at call time (patchable).
    return _policy.load_channel_context_blacklist(serving_channel_id,
                                                  access_file=ACCESS_FILE)


def _bot_token():
    """Resolve DISCORD_BOT_TOKEN via the shared policy (never printed). None = absent."""
    from channel_token import resolve_channel_token
    return resolve_channel_token("DISCORD_BOT_TOKEN", env_file=ENV_FILE) or None


def resolve_guild(target_channel_id, token):
    """Delegates to the shared policy; kept as a module attribute for stubbing."""
    return _policy.resolve_guild(target_channel_id, token)


def gate(serving_channel_id, target_channel_id, token):
    """Shared gate; guild resolver and ACCESS_FILE bind through THIS module."""
    return _policy.gate(serving_channel_id, target_channel_id, token,
                        guild_resolver=lambda t, tok: resolve_guild(t, tok),
                        access_file=ACCESS_FILE)


def _api_get(path, token):
    # Discord's edge (Cloudflare) 403s requests with urllib's default
    # "Python-urllib/x" User-Agent — a real UA is mandatory for the bot API.
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/sonichi/sutando, 1.0)",
    })
    # 429 Retry-After + transient 5xx backoff so a rate limit doesn't abort the read.
    return request_json(req, timeout=10)


def fetch_messages(target_channel_id, n, token):
    """Return a printable string of the N most recent messages. Isolated so the
    test can stub it. Rendering goes through the shared reader, so forwards and
    reply context are visible here exactly as in discord-read.py."""
    msgs = _api_get(f"/channels/{target_channel_id}/messages?limit={int(n)}", token)
    out = []
    for m in reversed(msgs):  # oldest-first reads naturally
        author = (m.get("author") or {}).get("username", "?")
        out.append(f"[{author}] {_reader._render(m)}")
        ctx = _reader._reply_context(m)
        if ctx:
            out.append(f"    {ctx}")
    return "\n".join(out) if out else "(no messages)"


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
