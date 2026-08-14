#!/usr/bin/env python3
"""Send a progress update to the channel a task originated from.

Usage:
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --message "On it, back shortly."
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --thread-ts 1780586204.198 --message "Still working..."
    python3 notify.py --source discord --channel-id 1234567890 --message "Working on it..."
    python3 notify.py --source telegram --chat-id 123456789 --message "On it..."
    python3 notify.py --source telegram --chat-id 123456789 --thread-id 42 --message "Still working..."
    python3 notify.py --source <provider> --channel-id '!roomid:server' --message "On it..."

Any --source other than slack/discord/telegram is treated as a remote-gateway
channel: the sender reads channels/<source>/.env (under $CLAUDE_CONFIG_DIR) for
REMOTE_TASK_URL + REMOTE_TASK_TOKEN and posts the message through the gateway's
POST /v1/room {op: "message"} endpoint — the same transport the task bridge for
that provider uses, so progress updates land in the originating room.

Exits 0 on success, 1 on failure. Fail-open by design — a failed send must never
block the task itself. The caller should always continue working regardless of exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional


MAX_PROGRESS_CHARS = 280
MAX_PROGRESS_LINES = 4
_DISCORD_USER_MENTION_RE = re.compile(r"<@!?([0-9]{17,20})>")
_PLAIN_AT_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.-]{2,64})")


def _progress_message_error(message: str) -> str | None:
    """Return a validation error when a notify body looks like a final answer."""
    stripped = message.strip()
    if len(stripped) > MAX_PROGRESS_CHARS:
        return (
            f"progress update is too long ({len(stripped)} chars; "
            f"max {MAX_PROGRESS_CHARS})"
        )
    line_count = len([line for line in stripped.splitlines() if line.strip()])
    if line_count > MAX_PROGRESS_LINES:
        return (
            f"progress update has too many lines ({line_count}; "
            f"max {MAX_PROGRESS_LINES})"
        )
    return None


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
    # Mirrors util_paths.claude_home_path ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude).
    _base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
    _claude_config = Path(_base) if _base else Path.home() / ".claude"
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


def _discord_request(url: str, token: str, payload: Optional[dict] = None):
    """Make a Discord JSON request and return its response body, or None."""
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/sonichi/sutando, 1.0)",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[task-progress] Discord request failed: {e}", file=sys.stderr)
        return None


def _discord_mentions(message: str):
    """Return (structured user ids, unresolved plain @handles), preserving order."""
    user_ids = list(dict.fromkeys(_DISCORD_USER_MENTION_RE.findall(message)))
    without_structured = _DISCORD_USER_MENTION_RE.sub("", message)
    plain_handles = list(dict.fromkeys(_PLAIN_AT_MENTION_RE.findall(without_structured)))
    return user_ids, plain_handles


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


def send_discord(channel_id: str, message: str, validate_mentions: bool = True) -> bool:
    token = _token("discord", "DISCORD_BOT_TOKEN")
    if not token:
        print("[task-progress] DISCORD_BOT_TOKEN not found", file=sys.stderr)
        return False
    user_ids, plain_handles = _discord_mentions(message)
    if validate_mentions and plain_handles:
        rendered = ", ".join(f"@{handle}" for handle in plain_handles)
        print(
            "[task-progress] unresolved Discord mention(s): "
            f"{rendered}. Use <@USER_ID>, or --no-validate-mentions for "
            "intentional plain-text handles.",
            file=sys.stderr,
        )
        return False

    if validate_mentions:
        for user_id in user_ids:
            resolved = _discord_request(
                f"https://discord.com/api/v10/users/{user_id}", token
            )
            if not isinstance(resolved, dict) or str(resolved.get("id")) != user_id:
                print(
                    f"[task-progress] Discord mention <@{user_id}> did not resolve; "
                    "message was not sent.",
                    file=sys.stderr,
                )
                return False

    payload = {
        "content": message,
        "allowed_mentions": {
            "parse": [],
            "users": user_ids,
            "replied_user": False,
        },
    }
    posted = _discord_request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        token,
        payload,
    )
    if not isinstance(posted, dict):
        return False

    if validate_mentions:
        resolved_ids = {
            str(mention.get("id"))
            for mention in posted.get("mentions", [])
            if isinstance(mention, dict)
        }
        missing = [user_id for user_id in user_ids if user_id not in resolved_ids]
        if missing:
            rendered = ", ".join(f"<@{user_id}>" for user_id in missing)
            print(
                "[task-progress] Discord posted the message but did not resolve "
                f"expected mention(s): {rendered}.",
                file=sys.stderr,
            )
            return False
    return True


def send_telegram(chat_id: str, message: str, message_thread_id: str | None = None) -> bool:
    token = _token("telegram", "TELEGRAM_BOT_TOKEN")
    if not token:
        print("[task-progress] TELEGRAM_BOT_TOKEN not found", file=sys.stderr)
        return False
    payload = {"chat_id": chat_id, "text": message}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return _post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        payload,
        {},
    )


# `source` is untrusted input that becomes a path segment: safe slug only, dots
# only BETWEEN alphanumerics, so every traversal shape is rejected up front.
_SOURCE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]|\.(?=[a-z0-9]))*$")


def send_remote_gateway(source: str, channel_id: str, message: str) -> bool:
    """Generic sender for gateway-bridged channels (any --source with a
    channels/<source>/.env carrying REMOTE_TASK_URL + REMOTE_TASK_TOKEN)."""
    if not _SOURCE_SLUG_RE.match(source or ""):
        print(f"[task-progress] invalid gateway source {source!r} — "
              "provider names are lowercase slugs; dots allowed only between "
              "alphanumerics (e.g. dev.ag2.space)", file=sys.stderr)
        return False
    # Mirrors util_paths.claude_home_path ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude).
    _base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
    _claude_config = Path(_base) if _base else Path.home() / ".claude"
    channels_dir = _claude_config / "channels"
    env_path = channels_dir / source / ".env"
    # Belt and suspenders: even a slug-valid name must RESOLVE inside the
    # channels directory. The containment root is the realpath of channels/
    # itself (so a symlinked channels dir works), but a channel entry that
    # symlinks OUT of the directory is refused by design.
    # Derive the EFFECTIVE gateway config from os.environ ALONE first — including
    # the alias and the combined "url|secret" one-token form. Only if that is still
    # missing a value do we resolve/guard/read the channel file. Checking just the
    # split REMOTE_TASK_URL+REMOTE_TASK_TOKEN pair was not enough: the documented
    # one-token onboarding (REMOTE_TASK_TOKEN=https://gw|secret, or the legacy
    # AG2_REMOTE_TOKEN) is a fully env-configured send, and it was still being
    # refused by the containment guard over a file it never needed.
    #
    # One-token onboarding: the URL travels inside the token — the same contract
    # ag2-sparrow's remote_gateway_bridge accepts (docs/remote-gateway-protocol.md).
    def _derive(get):
        u = (get("REMOTE_TASK_URL") or "").strip().rstrip("/")
        tok = (get("REMOTE_TASK_TOKEN") or "").strip()
        if not tok:
            tok = (get("AG2_REMOTE_TOKEN") or "").strip()
        if "|" in tok:
            _u, tok = tok.split("|", 1)
            if not u:
                u = _u.rstrip("/")
        if not u:
            u = (get("AG2_REMOTE_URL") or "").strip().rstrip("/")
        return u, tok

    url, token = _derive(lambda k: os.environ.get(k, ""))
    if not (url and token):
        # The file IS needed, so the containment check applies — unchanged. A
        # channel entry that symlinks OUT of channels/ is refused by design.
        real_env = os.path.realpath(env_path)
        real_root = os.path.realpath(channels_dir)
        if not real_env.startswith(real_root + os.sep):
            print(f"[task-progress] refusing env path outside channels dir: {env_path}",
                  file=sys.stderr)
            return False
        env = _env_file(real_env)
        url, token = _derive(lambda k: os.environ.get(k, "") or env.get(k, ""))
    if not url or not token:
        print(f"[task-progress] no REMOTE_TASK_URL/REMOTE_TASK_TOKEN (or AG2_REMOTE_TOKEN) "
              f"for source '{source}' (looked in {env_path})", file=sys.stderr)
        return False
    return _post(
        f"{url}/v1/room",
        {"op": "message", "room_id": channel_id, "body": message},
        {"Authorization": f"Bearer {token}",
         # some gateway edges (CDN/WAF) reject the default Python-urllib UA
         "User-Agent": "sutando-task-progress/1.0"},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a task-progress update to a channel.")
    parser.add_argument("--source", required=True,
                        help="Channel source: slack / discord / telegram, or any "
                             "gateway-bridged provider with a channels/<source>/.env")
    parser.add_argument("--channel-id", help="Slack / Discord channel ID")
    parser.add_argument("--chat-id", help="Telegram chat ID (alias for --channel-id on telegram)")
    parser.add_argument("--thread-ts", default=None,
                        help="Slack thread timestamp for threaded replies")
    parser.add_argument("--thread-id", default=None,
                        help="Telegram forum message_thread_id for threaded replies")
    parser.add_argument(
        "--no-validate-mentions",
        action="store_true",
        help="Discord only: allow intentional plain-text @handles and skip mention checks",
    )
    parser.add_argument("--message", required=True, help="Text to send")
    args = parser.parse_args()

    source = args.source
    message = args.message
    channel = args.channel_id or args.chat_id

    if not channel:
        print("[task-progress] --channel-id (or --chat-id) is required", file=sys.stderr)
        return 1

    validation_error = _progress_message_error(message)
    if validation_error:
        print(
            "[task-progress] refusing message: "
            f"{validation_error}. notify.py is only for short progress updates; "
            "write final answers to the task result file.",
            file=sys.stderr,
        )
        return 1

    if source == "slack":
        ok = send_slack(channel, message, thread_ts=args.thread_ts)
    elif source == "discord":
        ok = send_discord(
            channel,
            message,
            validate_mentions=not args.no_validate_mentions,
        )
    elif source == "telegram":
        ok = send_telegram(channel, message, message_thread_id=args.thread_id)
    else:
        ok = send_remote_gateway(source, channel, message)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
