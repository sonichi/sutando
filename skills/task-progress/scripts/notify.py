#!/usr/bin/env python3
"""Send a progress update to the channel a task originated from.

Usage:
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --message "On it, back shortly."
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --thread-ts 1780586204.198 --message "Still working..."
    python3 notify.py --source discord --channel-id 1234567890 --message "Working on it..."
    python3 notify.py --source telegram --chat-id 123456789 --message "On it..."

Exits 0 on success, 1 on failure. Fail-open by design — a failed send must never
block the task itself. The caller should always continue working regardless of exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def _env_file(path: str) -> dict[str, str]:
    """Parse key=value pairs from an .env file. Returns {} on any error."""
    result: dict[str, str] = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return result


def _token(source: str, var: str) -> str:
    """Resolve a token from env, then the channel .env file."""
    val = os.environ.get(var, "").strip()
    if val:
        return val
    _claude_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    env_path = _claude_config / "channels" / source / ".env"
    return _env_file(str(env_path)).get(var, "")


def _post(url: str, payload: dict, headers: dict) -> bool:
    """POST JSON payload. Returns True on 2xx."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            **headers,
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            # Slack returns {"ok": true/false}; Discord/Telegram return the message object.
            if isinstance(body, dict) and "ok" in body:
                return bool(body.get("ok"))
            return True
    except Exception as e:
        print(f"[task-progress] send failed: {e}", file=sys.stderr)
        return False


def send_slack(channel_id: str, message: str, thread_ts: str | None = None) -> bool:
    token = _token("slack", "SLACK_BOT_TOKEN")
    if not token:
        print("[task-progress] SLACK_BOT_TOKEN not found", file=sys.stderr)
        return False
    payload: dict = {"channel": channel_id, "text": message}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return _post(
        "https://slack.com/api/chat.postMessage",
        payload,
        {"Authorization": f"Bearer {token}"},
    )


def send_discord(channel_id: str, message: str) -> bool:
    token = _token("discord", "DISCORD_BOT_TOKEN")
    if not token:
        print("[task-progress] DISCORD_BOT_TOKEN not found", file=sys.stderr)
        return False
    return _post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        {"content": message},
        # Discord's API (behind Cloudflare) 403s the default `Python-urllib/x`
        # User-Agent with Cloudflare error 1010. Discord requires the
        # `DiscordBot ($url, $version)` UA format — send it or every POST fails.
        {"Authorization": f"Bot {token}",
         "User-Agent": "DiscordBot (https://github.com/sonichi/sutando, 1.0)"},
    )


def send_telegram(chat_id: str, message: str) -> bool:
    token = _token("telegram", "TELEGRAM_BOT_TOKEN")
    if not token:
        print("[task-progress] TELEGRAM_BOT_TOKEN not found", file=sys.stderr)
        return False
    return _post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": message},
        {},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a task-progress update to a channel.")
    parser.add_argument("--source", required=True, choices=["slack", "discord", "telegram"],
                        help="Channel source (slack / discord / telegram)")
    parser.add_argument("--channel-id", help="Slack / Discord channel ID")
    parser.add_argument("--chat-id", help="Telegram chat ID (alias for --channel-id on telegram)")
    parser.add_argument("--thread-ts", default=None,
                        help="Slack thread timestamp for threaded replies")
    parser.add_argument("--message", required=True, help="Text to send")
    args = parser.parse_args()

    source = args.source
    message = args.message
    channel = args.channel_id or args.chat_id

    if not channel:
        print("[task-progress] --channel-id (or --chat-id) is required", file=sys.stderr)
        return 1

    if source == "slack":
        ok = send_slack(channel, message, thread_ts=args.thread_ts)
    elif source == "discord":
        ok = send_discord(channel, message)
    elif source == "telegram":
        ok = send_telegram(channel, message)
    else:
        print(f"[task-progress] unknown source: {source}", file=sys.stderr)
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
