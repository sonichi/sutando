#!/usr/bin/env python3
"""Send a progress update to the channel a task originated from.

Usage:
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --message "On it, back shortly."
    python3 notify.py --source slack --channel-id D0B5L7X2TK2 --thread-ts 1780586204.198 --message "Still working..."
    python3 notify.py --source discord --channel-id 1234567890 --message "Working on it..."
    python3 notify.py --source telegram --chat-id 123456789 --message "On it..."
    python3 notify.py --source <provider> --channel-id '!roomid:server' --message "On it..."
    python3 notify.py --task-file "$WORKSPACE/tasks/task-123.txt" --message "On it..."

With --task-file, --source, --channel-id/--chat-id, --thread-root and --thread-ts
are read from that task file's headers; any of them given explicitly wins.

Any --source other than slack/discord/telegram is treated as a remote-gateway
channel: the sender reads channels/<source>/.env (under $CLAUDE_CONFIG_DIR) for
REMOTE_TASK_URL + REMOTE_TASK_TOKEN and posts the message through the gateway's
POST /v1/room {op: "message"} endpoint — the same transport the task bridge for
that provider uses, so progress updates land in the originating room. That file
must resolve inside channels/ itself, or be the AG2 Space desktop app's own
$SUTANDO_APP_SUPPORT/channels/<source>/.env (containment policy owned by
src/channel_env_containment.py; see _channel_env_is_contained below).

Exits 0 on success, 1 on failure. Fail-open by design — a failed send must never
block the task itself. The caller should always continue working regardless of exit code.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# parents[3] rather than a .parent chain so the workspace lint does not
# conflate this import bootstrap with workspace-path resolution.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
try:
    from policy.egress.unfurl import should_unfurl as _should_unfurl
except ImportError:  # skill running without the core tree
    def _should_unfurl(_body: str) -> bool:
        return False


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


def _channel_env_path(source: str) -> Path:
    # Mirrors util_paths.claude_home_path ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude).
    _base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
    _claude_config = Path(_base) if _base else Path.home() / ".claude"
    return _claude_config / "channels" / source / ".env"


def _load_resolver():
    """The bridges' own resolver, or None when src/ is not importable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
        from channel_token import resolve_channel_token  # type: ignore
        return resolve_channel_token
    except Exception:
        return None


_resolve_channel_token = _load_resolver()


def _token(source: str, var: str) -> str:
    """Resolve a token: process env -> channel `.env` -> vault.

    Delegates to channel_token so these tiers cannot drift from the bridges'.
    Degrades to the first two tiers alone rather than failing a notification.
    """
    env_path = _channel_env_path(source)
    if _resolve_channel_token is not None:
        return _resolve_channel_token(var, env_file=env_path)
    val = os.environ.get(var, "").strip()
    return val or _env_file(str(env_path)).get(var, "")


def _post(url: str, payload: dict, headers: dict, timeout: float = 10) -> bool:
    """POST JSON payload. Returns True on 2xx."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            **headers,
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            # Slack returns {"ok": true/false}; Discord/Telegram return the message object.
            if isinstance(body, dict) and "ok" in body:
                return bool(body.get("ok"))
            return True
    except Exception as e:
        print(f"[task-progress] send failed: {e}", file=sys.stderr)
        return False


def _rest_client(token: str):
    """The shared Discord chokepoint + injected post-gate. Resolved and
    imported lazily so non-Discord sources never touch the Discord stack."""
    repo = next(p for p in Path(__file__).resolve().parents
                if (p / "src" / "channels" / "discord" / "client.py").is_file())
    src = str(repo / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from channels.discord.post_gate import make_client
    return make_client(token, timeout=10)


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
    # Same owner as the bridge's send path decides this.
    unfurl = _should_unfurl(message)
    payload: dict = {"channel": channel_id, "text": message,
                     "unfurl_links": unfurl, "unfurl_media": unfurl}
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

    client = _rest_client(token)
    from outbox import DeliveryOutcome  # importable once _rest_client set the path

    if validate_mentions:
        for user_id in user_ids:
            try:
                resolved = client.get_user(user_id)
            except Exception as e:
                print(f"[task-progress] Discord request failed: {e}", file=sys.stderr)
                resolved = None
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
    receipt, _status, posted = client.send_message_with_response(channel_id, payload)
    if receipt.outcome is not DeliveryOutcome.CONFIRMED or not isinstance(posted, dict):
        print(
            f"[task-progress] Discord send not confirmed "
            f"({receipt.outcome.value}: {receipt.detail})",
            file=sys.stderr,
        )
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


# `source` is untrusted input that becomes a path segment: safe slug only, dots
# only BETWEEN alphanumerics, so every traversal shape is rejected up front.
_SOURCE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]|\.(?=[a-z0-9]))*$")


def _load_channel_env_containment():
    """The shared containment policy (src/channel_env_containment.py), or a
    fail-closed stub when src/ isn't importable this way — never silently
    widen the guard just because the import failed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
        from channel_env_containment import channel_env_is_contained  # type: ignore
        return channel_env_is_contained
    except Exception:
        return lambda env_path, channels_dir, source: False


# Single shared owner: src/channel_env_containment.py (see its docstring for
# the accept/refuse rule; also delegated to by core-supervisor-relay.py).
_channel_env_is_contained = _load_channel_env_containment()


def _gateway_config(source: str) -> "tuple[str, str] | None":
    """(url, token) for a gateway-bridged source, or None (reason already
    printed). Shared by the text and media senders so the two cannot drift."""
    if not _SOURCE_SLUG_RE.match(source or ""):
        print(f"[task-progress] invalid gateway source {source!r} — "
              "provider names are lowercase slugs; dots allowed only between "
              "alphanumerics (e.g. dev.ag2.space)", file=sys.stderr)
        return None
    # Mirrors util_paths.claude_home_path ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude).
    _base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
    _claude_config = Path(_base) if _base else Path.home() / ".claude"
    channels_dir = _claude_config / "channels"
    env_path = channels_dir / source / ".env"
    # Belt and suspenders: even a slug-valid name must RESOLVE inside an
    # approved root (_channel_env_is_contained) — anything else is refused.
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
        # The file IS needed, so the containment check applies — two approved
        # roots, fail-closed outside both (see _channel_env_is_contained).
        if not _channel_env_is_contained(env_path, channels_dir, source):
            print(f"[task-progress] refusing env path outside channels dir: {env_path}",
                  file=sys.stderr)
            return None
        env = _env_file(os.path.realpath(env_path))
        url, token = _derive(lambda k: os.environ.get(k, "") or env.get(k, ""))
    if not url or not token:
        print(f"[task-progress] no REMOTE_TASK_URL/REMOTE_TASK_TOKEN (or AG2_REMOTE_TOKEN) "
              f"for source '{source}' (looked in {env_path})", file=sys.stderr)
        return None
    return url, token


def _gateway_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            # some gateway edges (CDN/WAF) reject the default Python-urllib UA
            "User-Agent": "sutando-task-progress/1.0"}


def _event_id_or_empty(raw: str | None, field: str) -> "str | None":
    """`raw` stripped, or None on a refused malformed id. Only an empty string
    (unset $var expanded by the caller) is a legitimate opt-out; whitespace-only
    is refused, matching agent-room-ops relations._event_id."""
    text = "" if raw is None else str(raw)
    value = text.strip()
    if text and (not value.startswith("$") or len(value) < 2):
        print(f"[task-progress] {field} must be a Matrix event id like $abc, "
              f"got {text!r}", file=sys.stderr)
        return None
    return value


def send_remote_gateway(source: str, channel_id: str, message: str,
                        thread_root: str | None = None, reply_to: str | None = None) -> bool:
    """Generic sender for gateway-bridged channels (any --source with a
    channels/<source>/.env carrying REMOTE_TASK_URL + REMOTE_TASK_TOKEN).

    `thread_root` nests the post in an existing Matrix thread; `reply_to` only
    cites the message in the main timeline (`m.in_reply_to`, no `rel_type`) --
    the same distinction agent-room-ops/relations.py draws. A top-level ask
    (no `thread_root`) gets `reply_to` so the update still names what it is
    about without starting a thread under a message the user never threaded."""
    thread_root = _event_id_or_empty(thread_root, "thread_root")
    if thread_root is None:
        return False
    reply_to = _event_id_or_empty(reply_to, "reply_to")
    if reply_to is None:
        return False
    cfg = _gateway_config(source)
    if cfg is None:
        return False
    url, token = cfg
    # Progress updates carry the same worker stamp as results, so a notify
    # renders with the sender's attribution instead of stripping it.
    worker = os.environ.get("SUTANDO_WORKER_ID") or (
        f"worker-{os.environ.get('SUTANDO_WORKER_SEAT') or os.environ['SUTANDO_CORE_ID']}"
        if os.environ.get("SUTANDO_WORKER_SEAT") or os.environ.get("SUTANDO_CORE_ID") else None)
    payload = {"op": "message", "room_id": channel_id, "body": message}
    if worker:
        payload["extra_content"] = {"space.ag2.worker": {"id": worker}}
    # Independent fields (relations.py's relation_fields): thread_root nests
    # the post, reply_to is its citation fallback or the only relation at all.
    if thread_root:
        payload["thread_root"] = thread_root
    if reply_to:
        payload["reply_to"] = reply_to
    return _post(f"{url}/v1/room", payload, _gateway_headers(token))


# The gateway's own cap (remote_gateway_bridge MAX_MEDIA_BYTES), same env knob.
MAX_MEDIA_BYTES = int(os.environ.get("REMOTE_MEDIA_MAX_BYTES") or str(25 * 1024 * 1024))


def _load_attachment_policy():
    """The shared `[file:]` allowlist (src/policy/egress/attachment.py), or a
    fail-closed stub: an unimportable policy never widens what may be sent."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
        from policy.egress.attachment import is_path_sendable  # type: ignore
        return is_path_sendable
    except Exception:
        return lambda fpath, extra_roots=(): False


_is_path_sendable = _load_attachment_policy()


def upload_room_media(source: str, channel_id: str, path: str,
                      extra_roots: "tuple[str, ...]" = ()) -> "tuple[bool, str]":
    """Upload one local file into the originating room through the gateway's
    `POST /v1/rooms/<room>/media` — the route the task bridge's `[file:]`
    markers take, under the same allowlist (policy/egress/attachment.py) and
    size cap. Gateway sources only: Slack/Discord/Telegram progress updates
    stay text. Returns (ok, reason); the reason is already human-readable."""
    if source in ("slack", "discord", "telegram"):
        return False, f"media steps are not supported on {source}; the text step was sent"
    cfg = _gateway_config(source)
    if cfg is None:
        return False, "no gateway config"
    url, token = cfg
    fpath = os.path.realpath(os.path.expanduser((path or "").strip()))
    if not _is_path_sendable(fpath, extra_roots):
        return False, f"path not allowlisted: {fpath}"
    try:
        size = os.path.getsize(fpath)
        if size > MAX_MEDIA_BYTES:
            return False, f"file exceeds {MAX_MEDIA_BYTES} bytes"
        with open(fpath, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return False, f"read failed: {e}"
    safe_room = urllib.parse.quote(channel_id, safe="")
    ok = _post(f"{url}/v1/rooms/{safe_room}/media",
               {"filename": os.path.basename(fpath), "content_b64": content_b64},
               _gateway_headers(token), timeout=60)
    return (True, "") if ok else (False, "upload failed")


def _load_progress_route():
    """The shared route verdict (src/progress_route.py), or a fail-closed stub:
    an unimportable policy routes nothing rather than guessing."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
        import progress_route  # type: ignore
        return progress_route.delivery_route, progress_route.no_route_message, \
            progress_route.NO_ROUTE_EXIT
    except Exception:
        return (lambda source, channel: None,
                lambda source, channel: f"[task-progress] route policy unavailable; "
                                        f"{source!r} / {channel!r} not sent", 3)


_delivery_route, _no_route_message, NO_ROUTE_EXIT = _load_progress_route()


def _derive_from_task_file(path: str) -> dict:
    """source/channel_id/chat_id/thread_root/reply_to/thread_ts from a task file's headers.
    {} (with a stderr note) when the file is unreadable: explicit flags still apply."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[task-progress] --task-file unreadable ({e}); "
              f"falling back to explicit flags only", file=sys.stderr)
        return {}
    try:
        from local_task_protocol import parse_task_headers_lenient  # noqa: E402
    except ImportError:
        print("[task-progress] local_task_protocol unavailable; "
              "falling back to explicit flags only", file=sys.stderr)
        return {}
    # Lenient: producers put headers before or after `task:` (ag2space DM
    # envelopes write thread_root after it); first occurrence wins.
    headers = parse_task_headers_lenient(text).headers
    out = {
        "source": headers.get("source"),
        "channel_id": headers.get("channel_id") or headers.get("source_room_id"),
        "chat_id": headers.get("chat_id"),
        # Only a REAL thread_root header nests the post; the asking message
        # (never reply_to_event, the post the sender quoted) is always the citation.
        "thread_root": headers.get("thread_root"),
        "reply_to": headers.get("source_message_id"),
        # The Slack bridge writes reply_thread_ts; thread_ts is the generic key.
        "thread_ts": headers.get("reply_thread_ts") or headers.get("thread_ts"),
    }
    return {k: v for k, v in out.items() if v}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a task-progress update to a channel.")
    parser.add_argument("--task-file", default=None,
                        help="Path to the task file being processed; derives --source, "
                             "--channel-id/--chat-id, --thread-root, --reply-to and "
                             "--thread-ts from its headers so none of them need to be passed "
                             "(or remembered) by hand. Any of those flags given explicitly "
                             "still overrides what the file carries.")
    parser.add_argument("--source", default=None,
                        help="Channel source: slack / discord / telegram, or any "
                             "gateway-bridged provider with a channels/<source>/.env. "
                             "Required unless --task-file supplies one.")
    parser.add_argument("--channel-id", help="Slack / Discord channel ID")
    parser.add_argument("--chat-id", help="Telegram chat ID (alias for --channel-id on telegram)")
    parser.add_argument("--thread-ts", default=None,
                        help="Slack thread timestamp for threaded replies")
    parser.add_argument("--thread-root", default=None,
                        help="Gateway sources (e.g. ag2space): thread event id ($...) to nest "
                             "the post in. Only set this when the ask was already in a "
                             "thread -- threading is a decision, not this script's default.")
    parser.add_argument("--reply-to", default=None,
                        help="Gateway sources: event id ($...) this update is about, cited "
                             "in the main timeline (not a thread). Defaults to the task's "
                             "asking message via --task-file.")
    parser.add_argument(
        "--no-validate-mentions",
        action="store_true",
        help="Discord only: allow intentional plain-text @handles and skip mention checks",
    )
    parser.add_argument("--message", required=True, help="Text to send")
    args = parser.parse_args()

    derived = _derive_from_task_file(args.task_file) if args.task_file else {}

    # An explicit flag wins even when empty: --thread-root '' opts out of the file's thread.
    def _pick(explicit, fallback):
        return explicit if explicit is not None else fallback

    source = _pick(args.source, derived.get("source"))
    message = args.message
    explicit_channel = args.channel_id if args.channel_id is not None else args.chat_id
    channel = _pick(explicit_channel, derived.get("channel_id") or derived.get("chat_id"))
    thread_root = _pick(args.thread_root, derived.get("thread_root"))
    reply_to = _pick(args.reply_to, derived.get("reply_to"))
    thread_ts = _pick(args.thread_ts, derived.get("thread_ts"))

    if not source:
        print("[task-progress] --source is required (directly, or derivable from --task-file)",
              file=sys.stderr)
        return 1

    if _delivery_route(source, channel) is None:
        print(_no_route_message(source, channel), file=sys.stderr)
        return NO_ROUTE_EXIT

    if not channel:
        print("[task-progress] --channel-id (or --chat-id) is required "
              "(directly, or derivable from --task-file)", file=sys.stderr)
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
        ok = send_slack(channel, message, thread_ts=thread_ts)
    elif source == "discord":
        ok = send_discord(
            channel,
            message,
            validate_mentions=not args.no_validate_mentions,
        )
    elif source == "telegram":
        ok = send_telegram(channel, message)
    else:
        ok = send_remote_gateway(source, channel, message, thread_root=thread_root, reply_to=reply_to)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
