#!/usr/bin/env python3
"""Post one message to a named Discord channel and prove it carried a mention.

Separate from bot2bot-post because that tool always resolves the bot2bot
channel: routing a reviewer notification through it validated one channel and
delivered to another.

Sends through the shared client, which is the repo's only sanctioned Discord
sender — a hand-rolled one skips the post-gate validator the client applies.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
from channels.discord.client import DiscordRestClient  # noqa: E402


def token() -> str:
    base = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (pathlib.Path.home() / ".claude"))
    for line in (base / "channels" / "discord" / ".env").read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("send_channel_message: DISCORD_BOT_TOKEN not found")


def send(channel: str, body: str, client=None):
    """-> (receipt, posted_body). `client` is injectable so the tests drive
    every outcome without a network."""
    client = client or DiscordRestClient(token())
    receipt, _status, posted = client.send_message_with_response(channel, {"content": body})
    return receipt, posted


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: send_channel_message.py <channel-id> <body>", file=sys.stderr)
        return 2
    channel, body = argv
    receipt, posted = send(channel, body)
    if not getattr(receipt, "delivered", False):
        print(f"send_channel_message: not delivered — {getattr(receipt, 'reason', receipt)}",
              file=sys.stderr)
        return 1
    # A mention is the delivery mechanism, and the API reports 200 either way.
    # Read it back from the POSTED body, never from the string we sent.
    if not ((posted or {}).get("mentions") or []):
        print(f"send_channel_message: posted {(posted or {}).get('id')} to {channel} but its "
              "mentions array is EMPTY — it notified nobody", file=sys.stderr)
        return 3
    print(posted["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
