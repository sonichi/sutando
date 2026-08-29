#!/usr/bin/env python3
"""Post one message to a named Discord channel and prove it reached ONE named user.

Separate from bot2bot-post because that tool always resolves the bot2bot
channel: routing a reviewer notification through it validated one channel and
delivered to another.

Constructs through `post_gate.make_client` and resolves its token through
`resolve_channel_token`: binding the client directly reaches the transport
while skipping the post-gate and the env -> .env -> vault contract.
"""
from __future__ import annotations

import os
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))
from channel_token import resolve_channel_token          # noqa: E402
from channels.discord.post_gate import make_client       # noqa: E402
from outbox import DeliveryOutcome                       # noqa: E402


def token() -> str:
    base = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (pathlib.Path.home() / ".claude"))
    tok = resolve_channel_token("DISCORD_BOT_TOKEN",
                                env_file=base / "channels" / "discord" / ".env")
    if not tok:
        raise SystemExit("send_channel_message: DISCORD_BOT_TOKEN not found")
    return tok


def send(channel: str, body: str, user_id: str, client=None):
    """-> (receipt, posted_body). `client` is injectable so the tests drive
    every outcome without a network. `allowed_mentions` is narrowed to
    `user_id`: the read-back can then only succeed for the intended target."""
    client = client or make_client(token(), repo_root=_REPO)
    payload = {"content": body,
               "allowed_mentions": {"parse": [], "users": [str(user_id)]}}
    receipt, _status, posted = client.send_message_with_response(channel, payload)
    return receipt, posted


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print("usage: send_channel_message.py <channel-id> <user-id> <body>", file=sys.stderr)
        return 2
    channel, user_id, body = argv
    receipt, posted = send(channel, body, user_id)
    outcome = getattr(receipt, "outcome", None)

    # Three outcomes, three meanings. Collapsing them is what turns a landed
    # post into a retry: the receipt is RetrySafety.UNSAFE, so a repeat duplicates.
    if outcome == DeliveryOutcome.NOT_DELIVERED:
        print(f"send_channel_message: NOT DELIVERED — {getattr(receipt, 'detail', '')}",
              file=sys.stderr)
        return 1
    if outcome != DeliveryOutcome.CONFIRMED:
        print(f"send_channel_message: OUTCOME UNKNOWN — the post MAY have landed "
              f"({getattr(receipt, 'detail', '')}). Do not retry blindly; check the "
              "channel before sending again.", file=sys.stderr)
        return 4

    # The API reports 200 either way, and a non-empty array proves only that
    # SOMEBODY was mentioned: require the id we were asked to reach.
    mentioned = {str(m.get("id")) for m in ((posted or {}).get("mentions") or [])
                 if isinstance(m, dict)}
    if str(user_id) not in mentioned:
        print(f"send_channel_message: posted {(posted or {}).get('id')} to {channel} but "
              f"{user_id} is not in its mentions {sorted(mentioned) or '[]'} — it "
              "notified someone else, or nobody", file=sys.stderr)
        return 3
    print((posted or {}).get("id") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
