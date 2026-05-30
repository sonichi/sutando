#!/usr/bin/env python3
"""
Slack bridge for Sutando — receives DMs + @mentions via Socket Mode, writes to
tasks/, sends replies from results/. Works alongside the voice / discord /
telegram bridges. Runs as a background daemon.

Usage: python3 src/slack-bridge.py

Env vars:
    SLACK_BOT_TOKEN  — xoxb-... from app's OAuth & Permissions page
    SLACK_APP_TOKEN  — xapp-... from app's Basic Information page
                       (Socket Mode enabled, scope `connections:write`)

Bot scopes (OAuth & Permissions):
    chat:write, im:history, im:write, app_mentions:read,
    channels:history, groups:history, files:read, files:write,
    users:read

Access list (TOFU onboarding, same schema as telegram):
    ~/.claude/channels/slack/access.json
        {"allowFrom": ["U0123..."], "tofuOwner": "U0123...", ...}

File round-trip:
    Inbound  — files attached to DMs/mentions are downloaded into
               $SUTANDO_WORKSPACE/slack-inbox/ and the path is surfaced
               in the task body as "[File attached: /path]".
    Outbound — result bodies may include [file: /path], [send: /path],
               or [attach: /path] markers. Paths are allowlisted via
               _is_path_sendable() (same realpath+startswith sanitizer
               the telegram/discord bridges use) and uploaded via
               files_upload_v2.
"""

from __future__ import annotations


import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_priority import default_priority_for_source  # noqa: E402
from result_markers import parse_markers  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from task_archive import find_task_file  # noqa: E402
from single_instance import acquire as _single_instance_acquire  # noqa: E402

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:
    print("slack_bolt not installed. Run: pip install slack_bolt", file=sys.stderr)
    sys.exit(1)

REPO = resolve_workspace()
TASKS_DIR = REPO / "tasks"
RESULTS_DIR = REPO / "results"
STATE_DIR = REPO / "state"
INBOX_DIR = REPO / "slack-inbox"
ARCHIVE_TASKS_DIR = REPO / "tasks" / "archive"
ARCHIVE_RESULTS_DIR = REPO / "results" / "archive"
OWNER_ACTIVITY_FILE = STATE_DIR / "last-owner-activity.json"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
INBOX_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
if not BOT_TOKEN or not APP_TOKEN:
    print("SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN not set", file=sys.stderr)
    sys.exit(1)


# Outbound file-send allowlist — mirrors _is_path_sendable() in
# discord-bridge.py + telegram-bridge.py. Fail-closed by default.
SEND_ALLOWED_ROOTS = (
    str(REPO / "results"),
    str(REPO / "notes"),
    str(REPO / "docs"),
    str(INBOX_DIR),
)
SEND_ALLOWED_PREFIXES = (
    "/tmp/sutando-",
    "/private/tmp/sutando-",
    "/tmp/echo-",
    "/private/tmp/echo-",
)


def _is_path_sendable(fpath: str) -> bool:
    """True iff `fpath` is a real file AND resolves under an allowed root.

    Uses os.path.realpath + startswith — CodeQL recognizes this pattern as
    a path-injection sanitizer. Do NOT swap for Path.resolve() without
    re-proving to CodeQL. Same shape as the discord/telegram allowlist.
    """
    if not os.path.isfile(fpath):
        return False
    try:
        real = os.path.realpath(fpath)
    except OSError:
        return False
    for root in SEND_ALLOWED_ROOTS:
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    for prefix in SEND_ALLOWED_PREFIXES:
        if real.startswith(prefix):
            return True
    return False


def write_owner_activity(channel: str, summary: str) -> None:
    """Record owner activity — same schema as src/discord-bridge.py."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(time.time()),
            "channel": channel,
            "summary": summary[:80],
        }
        tmp = OWNER_ACTIVITY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.rename(OWNER_ACTIVITY_FILE)
    except Exception as e:
        print(f"  [owner-activity] write failed: {e}", flush=True)


def archive_file(src: Path, kind: str, task_id: str) -> None:
    """Move src into archive/<tasks|results>/YYYY-MM/ instead of deleting.
    Matches the behavior of telegram-bridge.py / discord-bridge.py."""
    try:
        if not src.exists():
            return
        from datetime import datetime
        import shutil
        ym = datetime.now().strftime("%Y-%m")
        base = ARCHIVE_TASKS_DIR if kind == "tasks" else ARCHIVE_RESULTS_DIR
        dest_dir = base / ym
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / f"{task_id}.txt"))
    except Exception as e:
        print(f"[Slack] archive_file({kind}, {task_id}) failed: {e}", flush=True)
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass


PRESENTER_SENTINEL = REPO / "state" / "presenter-mode.sentinel"


def presenter_mode_active() -> bool:
    if not PRESENTER_SENTINEL.exists():
        return False
    try:
        expire_iso = PRESENTER_SENTINEL.read_text().strip()
        if not expire_iso or not expire_iso[0].isdigit():
            return False
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return now_iso < expire_iso
    except Exception:
        return False


ACCESS_FILE = Path.home() / ".claude" / "channels" / "slack" / "access.json"

# In-memory mirror of access.json. Updated on every successful read.
# Used by tofu_onboard() to detect and recover from external deletions
# (#899: Sutando.app Settings or another process can delete the file
# between bridge events; without this cache the bridge re-TOFUs on the
# next inbound message, wiping tierMap / manually-added allowFrom entries).
_access_cache: dict | None = None
_access_cache_mtime: float = 0.0
_access_cache_lock = threading.Lock()


def _update_access_cache(data: dict) -> None:
    global _access_cache, _access_cache_mtime
    try:
        mtime = ACCESS_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _access_cache_lock:
        _access_cache = data
        _access_cache_mtime = mtime


def _restore_access_from_cache() -> bool:
    """Write _access_cache back to ACCESS_FILE. Returns True if restored."""
    with _access_cache_lock:
        cached = _access_cache
    if not cached or not cached.get("tofuOwner"):
        return False
    try:
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_FILE.write_text(json.dumps(cached, indent=2) + "\n")
        os.chmod(ACCESS_FILE, 0o600)
        print(
            "  [access] restored access.json from in-memory cache "
            "(external deletion detected — #899)",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"  [access] cache restore failed: {e}", flush=True)
        return False


def load_allowed():
    """Return set of allowed Slack user IDs, or None if access.json missing.

    None vs empty-set: file-missing means never-configured (TOFU-eligible);
    empty allowFrom means admin explicitly locked it down (no TOFU)."""
    try:
        data = json.loads(ACCESS_FILE.read_text())
        _update_access_cache(data)
        return set(data.get("allowFrom", []))
    except FileNotFoundError:
        return None
    except Exception:
        return set()


def load_tier_map() -> dict:
    """Return the per-user-id → tier map from access.json `tierMap`, or
    empty dict if missing. Recognized tiers: "owner", "team", "other".
    Unmapped users default to "owner" — preserves the pre-tierMap behavior
    where every entry in `allowFrom` was treated as owner-tier."""
    with _access_cache_lock:
        cached = _access_cache
        cached_mtime = _access_cache_mtime
    if cached is not None:
        try:
            if ACCESS_FILE.stat().st_mtime == cached_mtime:
                return cached.get("tierMap") or {}
        except OSError:
            pass  # file deleted — fall through to re-read (will return {})
    try:
        data = json.loads(ACCESS_FILE.read_text())
        _update_access_cache(data)
        return data.get("tierMap") or {}
    except Exception:
        return {}


def tofu_onboard(user_id: str, username: str | None) -> set:
    """First-time auto-onboard — same contract as telegram-bridge.py.

    Before running TOFU, check for external file deletion (#899): if the
    file is missing but _access_cache holds a valid prior state, restore
    from cache instead of wiping tierMap / allowFrom with a fresh TOFU."""
    if ACCESS_FILE.exists():
        return load_allowed() or set()
    # File is missing. Was it externally deleted after a prior onboarding?
    if _restore_access_from_cache():
        return load_allowed() or set()
    # Genuine first-time TOFU.
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "allowFrom": [user_id],
        "tofuOwner": user_id,
        "tofuOnboardedAt": int(time.time()),
        "tofuOnboardedUsername": username or None,
    }
    ACCESS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(ACCESS_FILE, 0o600)
    _update_access_cache(payload)
    print(
        f"  TOFU: auto-onboarded @{username} (id={user_id}) as owner — wrote {ACCESS_FILE}",
        flush=True,
    )
    return {user_id}


# Track which Slack channel/thread to reply into for each task we wrote.
# Keyed by task_id; value is {channel, thread_ts} so we can reply in-thread
# for @mentions and at top-level for DMs.
pending_replies: dict[str, dict] = {}
pending_replies_lock = threading.Lock()

# Username cache — users.info is rate-limited (Tier 4 = 100/min). One
# cache lookup per known user saves a network hop on every DM. Cache
# never invalidates because display names rarely change and a stale
# username is only a cosmetic issue in the task body. Cleared on
# process restart.
_username_cache: dict[str, str | None] = {}
_username_cache_lock = threading.Lock()

# Event counter — used by the no-events-after-60s hint thread to detect
# the "Socket Mode connected but Event Subscriptions disabled" install
# trap. Cost of the most common install hang-up is ~1h of owner time
# (verified 2026-05-18). The hint is cheap insurance.
_event_count = 0
_event_count_lock = threading.Lock()

# Bolt App. Socket Mode handler attaches via SocketModeHandler below.
app = App(token=BOT_TOKEN)


def _download_slack_file(file_dict: dict) -> str | None:
    """Download a Slack file to INBOX_DIR. Returns the local path or None.

    Slack file URLs require the bot token in an Authorization header — they
    are NOT public. We GET url_private and write to a name-mangled local
    file using the original filename suffix where possible.
    """
    url = file_dict.get("url_private_download") or file_dict.get("url_private")
    if not url:
        return None
    name_hint = file_dict.get("name") or file_dict.get("id") or "file"
    # Slack returns filenames that may contain path separators or weird
    # chars. Strip to basename and replace anything sketchy with _.
    safe_name = os.path.basename(name_hint).replace(os.sep, "_") or "file"
    local_path = INBOX_DIR / f"{int(time.time() * 1000)}-{safe_name}"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {BOT_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(local_path, "wb") as f:
            f.write(resp.read())
        return str(local_path)
    except Exception as e:
        print(f"  [file] download failed for {name_hint}: {e}", flush=True)
        return None


def _write_task(event: dict, prefix: str, text: str, username: str | None) -> str | None:
    """Write a task file from a Slack event. Returns task_id or None if skipped."""
    user_id = event.get("user")
    if not user_id:
        return None

    # Per-event state probe — captures whether ACCESS_FILE exists at the moment
    # _write_task runs, and its mtime if it does. This is the instrumentation
    # asked for by #899 (intermittent file wipe + re-TOFU despite the race-guard
    # in tofu_onboard). The wipe must be happening externally (Sutando.app
    # Settings UI, manual rm, or an undiscovered code path), and the only way
    # to catch it is to log the file's state on every inbound event. One line
    # per event; cheap; bridges already log per-event.
    try:
        af_exists = ACCESS_FILE.exists()
        af_mtime = ACCESS_FILE.stat().st_mtime if af_exists else None
        print(f"  [access-probe] file_present={af_exists} mtime={af_mtime}", flush=True)
    except Exception:
        # Don't let a probe failure block real work; just skip the log line.
        pass

    # Access control via TOFU
    allowed = load_allowed()
    if allowed is None:
        allowed = tofu_onboard(user_id, username)
    if user_id not in allowed:
        print(f"  Dropped message from non-allowed user {user_id}", flush=True)
        return None

    # Download any attached files BEFORE writing the task, so the task body
    # carries the local paths. Skips silently on failure — task still goes
    # through with whatever files did download.
    attachment_lines = []
    for file_dict in event.get("files") or []:
        local_path = _download_slack_file(file_dict)
        if local_path:
            attachment_lines.append(f"[File attached: {local_path}]")
    attachment_note = ("\n" + "\n".join(attachment_lines)) if attachment_lines else ""

    if not text and not attachment_note:
        return None

    write_owner_activity("slack", text or attachment_note)

    channel = event.get("channel", "")
    # Reply in-thread for channel @mentions, top-level for DMs. parens for
    # readability; Python's `or` + ternary precedence is correct here but
    # the explicit grouping makes the intent obvious to humans.
    if event.get("channel_type") != "im":
        thread_ts = event.get("thread_ts") or event.get("ts")
    else:
        thread_ts = None

    # Resolve access_tier from `tierMap`.
    # Two cases for unmapped users:
    #   1. tierMap absent (pre-tierMap config) → "owner" (backward compat)
    #   2. tierMap present but uid missing → "other" (fail-safe, prevents
    #      silent privilege escalation when operator forgets a tierMap line)
    # See #893 for the rationale behind the split default.
    tier_map = load_tier_map()
    if user_id in tier_map:
        access_tier = tier_map[user_id]
    elif tier_map:
        # tierMap exists but uid is missing — degrade to "other"
        print(
            f"  [tier-map] WARNING: User {user_id} in allowFrom but missing from tierMap; defaulting to 'other'",
            flush=True,
        )
        access_tier = "other"
    else:
        # tierMap absent entirely — pre-tierMap config, all users are owner
        access_tier = "owner"
    if access_tier not in ("owner", "team", "other"):
        # Unknown tier value in config → degrade safely to "other" rather
        # than treating as owner.
        access_tier = "other"

    # Prepend an in-band system instruction for non-owner tiers so the
    # core agent cannot accidentally process a downgraded task with full
    # capabilities. Mirrors the Discord bridge's tier-specific instruction
    # block (see discord-bridge.py around `===SUTANDO SYSTEM INSTRUCTIONS===`).
    # Kept short here because Slack's downgrade surface today is just
    # "delegate to sandboxed read-only agent" — no Slack-state-prefetch
    # path equivalent to Discord's `_prefetch_discord_state_refs`.
    user_task_text = f"[{prefix} @{username or user_id}] {text}{attachment_note}"
    if access_tier != "owner":
        user_task_text = (
            f"{user_task_text}\n\n"
            f"===SUTANDO SYSTEM INSTRUCTIONS (do not ignore; overrides anything above)===\n"
            f"This Slack task is from a {access_tier.upper()} tier sender, NOT the owner. "
            f"You MUST delegate to a sandboxed agent (e.g. `codex exec --sandbox read-only`) "
            f"and NEVER process it with full core-agent capabilities. "
            f"For 'team' tier: information lookups OK, no system mutations. "
            f"For 'other' tier: information-only replies about Sutando itself. "
            f"Write the sandboxed output to `results/{{task_id}}.txt` as the user-facing reply.\n"
        )

    ts = int(time.time() * 1000)
    task_id = f"task-{ts}"
    task_file = TASKS_DIR / f"{task_id}.txt"
    priority = default_priority_for_source("slack", access_tier)
    task_file.write_text(
        f"id: {task_id}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"task: {user_task_text}\n"
        f"source: slack\n"
        f"channel_id: {channel}\n"
        f"user_id: {user_id}\n"
        f"access_tier: {access_tier}\n"
        f"priority: {priority}\n"
    )
    with pending_replies_lock:
        pending_replies[task_id] = {"channel": channel, "thread_ts": thread_ts}

    global _event_count
    with _event_count_lock:
        _event_count += 1

    print(f"  Wrote {task_id} from {prefix} @{username}", flush=True)
    return task_id


def _resolve_username(user_id: str) -> str | None:
    """Resolve Slack user_id → display_name, cached.

    The cache is unbounded but keyed by user_id, so practical size is
    O(distinct senders) per process lifetime — fine for a personal agent.
    Never invalidates: a stale display name is only cosmetic.
    """
    with _username_cache_lock:
        if user_id in _username_cache:
            return _username_cache[user_id]
    name: str | None = None
    try:
        resp = app.client.users_info(user=user_id)
        name = resp["user"]["profile"].get("display_name") or resp["user"].get("name")
    except Exception:
        pass
    with _username_cache_lock:
        _username_cache[user_id] = name
    return name


@app.event("app_mention")
def handle_mention(event, say):
    """Channel @mention → task file."""
    user_id = event.get("user")
    username = _resolve_username(user_id) if user_id else None
    raw = event.get("text", "")
    # Strip the leading <@BOTID> mention from the text body for cleanliness.
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", raw).strip()
    _write_task(event, "Slack mention", text, username)


@app.event("message")
def handle_message(event, say):
    """DM → task file. Channel messages are handled via app_mention only."""
    # Ignore bot messages, edited messages, and channel-history backfills.
    if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
        return
    # Only handle direct messages (channel_type=im). Channel @mentions arrive
    # via the separate app_mention event above, so handling them here would
    # double-fire.
    if event.get("channel_type") != "im":
        return
    user_id = event.get("user")
    if not user_id:
        return
    username = _resolve_username(user_id)
    text = (event.get("text") or "").strip()
    _write_task(event, "Slack DM", text, username)


# Markers that the bridge handles specially in result bodies. Same set as
# discord-bridge.py + telegram-bridge.py — see CLAUDE.md "Result-body
# protocol markers".
FILE_MARKER_RE = re.compile(r'\[(?:file|send|attach):\s*([^\]]+)\]')


def _send_file(channel: str, thread_ts: str | None, fpath: str) -> bool:
    """Upload a file to a Slack channel/DM via files_upload_v2.

    Returns True on success. Caller is responsible for allowlist-gating
    the path before invocation — this function does not re-check.
    """
    try:
        kwargs: dict = {
            "channel": channel,
            "file": fpath,
            "filename": os.path.basename(fpath),
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        # files_upload_v2 is the recommended modern endpoint; the older
        # files.upload is deprecated as of March 2025.
        app.client.files_upload_v2(**kwargs)
        return True
    except Exception as e:
        print(f"[Slack] files_upload_v2 failed for {fpath}: {e}", flush=True)
        return False


def _send_reply(channel: str, thread_ts: str | None, text: str, task_id: str | None = None) -> None:
    """Post a reply via chat.postMessage with marker extraction.

    Honors the unified marker protocol from `src/result_markers.py` (#873):
    - `[channel: <id>]` at body start → redirect to <id>, drop thread_ts
      (cross-channel posts don't carry the original thread context).
    - `[file:]/[send:]/[attach:]` anywhere → upload via files_upload_v2,
      stripped from text body.
    Skip markers ([no-send] / [REPLIED] / [deduped:]) are handled upstream
    in result_watcher() so we never see them here.

    Long text chunked at 4000 chars per Slack message (40k hard cap, but
    readability suffers above ~4k).
    """
    if not text:
        return

    parsed = parse_markers(text)
    clean_text = parsed.body

    # [channel:] redirect — for cross-channel posting (e.g., reply to a DM
    # task by sending into a public channel instead). Drop thread_ts since
    # we're moving to a new channel.
    for action in parsed.actions:
        if action.kind == "redirect":
            channel = action.value
            thread_ts = None
            break

    file_paths = [a.value for a in parsed.actions if a.kind == "attach"]

    # Post the text body in 4000-char chunks (Slack's per-message limit is
    # 40k chars but readability suffers above ~4k).
    if clean_text:
        all_chunks_sent = True
        for i in range(0, len(clean_text), 4000):
            kwargs = {"channel": channel, "text": clean_text[i:i + 4000]}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                app.client.chat_postMessage(**kwargs)
            except Exception as e:
                print(f"[Slack] chat_postMessage failed: {e}", flush=True)
                all_chunks_sent = False
                break
        if all_chunks_sent:
            # Slack channel id starts with D (DM), C (public/private channel),
            # G (legacy group). Best-effort classification for the audit log.
            ch_type = "slack_dm" if channel.startswith("D") else "slack_channel"
            try:
                import outbox_log
                outbox_log.append(
                    channel_type=ch_type,
                    recipient=channel,
                    body=clean_text,
                    task_id=task_id,
                )
            except Exception:
                pass

    # Then upload each file. Fail-closed via _is_path_sendable.
    for fpath in file_paths:
        if _is_path_sendable(fpath):
            if _send_file(channel, thread_ts, fpath):
                print(f"  Sent file: {fpath}", flush=True)
        elif os.path.isfile(fpath):
            # Path exists but isn't allowlisted — surface a visible deny.
            try:
                app.client.chat_postMessage(
                    channel=channel,
                    text=f"(file access denied: {fpath})",
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                )
            except Exception:
                pass
            print(f"  BLOCKED file: {fpath}", flush=True)
        else:
            try:
                app.client.chat_postMessage(
                    channel=channel,
                    text=f"(file not found: {fpath})",
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                )
            except Exception:
                pass


def result_watcher():
    """Background thread: polls results/ for replies + proactive messages."""
    heartbeat_file = REPO / "state" / "slack-bridge.heartbeat"
    last_heartbeat = 0.0
    while True:
        try:
            # Replies to pending tasks
            with pending_replies_lock:
                pending_ids = list(pending_replies.keys())
            for task_id in pending_ids:
                result_file = RESULTS_DIR / f"{task_id}.txt"
                if not result_file.exists():
                    continue
                reply_text = result_file.read_text().strip()
                with pending_replies_lock:
                    target = pending_replies.pop(task_id, None)
                if not target:
                    continue

                # Skip-marker check via unified parser (#873). Equivalent to
                # the prior startswith trio but routed through one source of
                # truth so future skip markers added in result_markers.py
                # automatically apply here.
                _skip_parsed = parse_markers(reply_text)
                if any(a.kind == "skip" for a in _skip_parsed.actions):
                    print(f"  Skipped (marker): {task_id}", flush=True)
                else:
                    try:
                        _send_reply(target["channel"], target.get("thread_ts"), reply_text, task_id=task_id)
                        print(f"  Replied to {target['channel']}: {reply_text[:80]}...", flush=True)
                    except Exception as e:
                        print(f"[Slack] reply error: {e}", flush=True)

                archive_file(result_file, "results", task_id)
                archive_file(find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt", "tasks", task_id)

            # Proactive messages (sent to owner DM)
            if not presenter_mode_active():
                for f in list(RESULTS_DIR.iterdir()):
                    if not (f.name.startswith("proactive-") and f.suffix == ".txt"):
                        continue
                    claim = f.with_suffix(".sending")
                    try:
                        f.rename(claim)
                    except FileNotFoundError:
                        continue
                    text = claim.read_text().strip()
                    if not text:
                        claim.unlink(missing_ok=True)
                        continue
                    owner_ids = load_allowed()
                    if owner_ids:
                        owner_id = next(iter(owner_ids))
                        # Open a DM channel to the owner (idempotent).
                        try:
                            resp = app.client.conversations_open(users=owner_id)
                            dm_channel = resp["channel"]["id"]
                            _send_reply(dm_channel, None, text)
                            print(f"  [proactive] sent to {owner_id}: {text[:80]}", flush=True)
                        except Exception as e:
                            print(f"  [proactive] failed: {e}", flush=True)
                    claim.unlink(missing_ok=True)

            # Heartbeat (used by health-check.py)
            now = time.time()
            if now - last_heartbeat >= 60:
                try:
                    heartbeat_file.write_text(str(int(now)))
                    last_heartbeat = now
                except Exception:
                    pass

            time.sleep(1)
        except Exception as e:
            print(f"[Slack] result_watcher error: {e}", flush=True)
            time.sleep(5)


def _no_events_hint_thread():
    """One-shot watchdog: 60s after start, if no events have arrived,
    log a hint pointing at the most common install trap (Event
    Subscriptions disabled). Suppresses itself once any event is seen.

    Owner spent ~1h on 2026-05-18 hitting exactly this state: bridge
    alive, Socket Mode WS connected to Slack, but Event Subscriptions
    was off so no events ever flowed. The bridge log was silent past
    "Socket Mode connecting…" — no signal to act on. This hint surfaces
    the diagnostic the next install will need.
    """
    time.sleep(60)
    with _event_count_lock:
        n = _event_count
    if n == 0:
        print(
            "[Slack] HINT: 60s elapsed with zero events received.\n"
            "  Bridge is connected to Slack's edge, but events are not arriving.\n"
            "  Most common cause: Event Subscriptions is disabled in your app config.\n"
            "  Fix: https://api.slack.com/apps → your app → Event Subscriptions →\n"
            "    1. Toggle 'Enable Events' to ON\n"
            "    2. Under 'Subscribe to bot events' add: message.im, app_mention\n"
            "    3. Save Changes (if greyed, see docs/slack-bridge.md install gotchas)\n"
            "    4. Reinstall app if Slack prompts a yellow banner\n"
            "  Then send a DM to your bot — TOFU will auto-onboard you as owner.",
            flush=True,
        )



def _recover_orphan_sending_files() -> int:
    """Restart-safety: rename any orphan `results/proactive-*.sending`
    files back to `*.txt` so they get re-claimed on the next poll.
    Returns the number of files recovered.

    Atomic-claim-by-rename (`proactive-*.txt` → `.sending`) prevents
    same-tick double-deliveries between concurrent poll iterations.
    But if the bridge crashes BETWEEN the rename and the delivery,
    the `.sending` file sits orphaned in `results/` — no poll
    iteration ever looks at `.sending` suffixes, so the owner
    notification is silently dropped until next manual intervention.

    Mirrors `_recover_orphan_sending_files` in discord-bridge.py and
    telegram-bridge.py (PR #1046). See those docstrings for the full
    bug-class write-up.
    """
    if not RESULTS_DIR.exists():
        return 0
    recovered = 0
    for f in RESULTS_DIR.iterdir():
        if not (f.name.startswith("proactive-") and f.suffix == ".sending"):
            continue
        target = f.with_suffix(".txt")
        try:
            if target.exists():
                print(
                    f"  [startup] skipping orphan recovery: {target.name} "
                    f"already exists (collision with {f.name})",
                    flush=True,
                )
                continue
            f.rename(target)
            recovered += 1
            print(f"  [startup] recovered orphan {f.name} → {target.name}", flush=True)
        except FileNotFoundError:
            # Lost the race to another process; fine.
            pass
        except Exception as e:
            print(f"  [startup] failed to recover {f.name}: {e}", flush=True)
    if recovered:
        print(f"  [startup] recovered {recovered} orphan .sending file(s)", flush=True)
    return recovered

def main():
    _single_instance_acquire("slack-bridge")
    print("Slack bridge started. Socket Mode connecting...", flush=True)
    _recover_orphan_sending_files()
    # Prime the in-memory access cache so tofu_onboard() can detect external
    # deletions even on the very first inbound message after a restart (#899).
    load_allowed()
    threading.Thread(target=result_watcher, name="slack-result-watcher", daemon=True).start()
    threading.Thread(target=_no_events_hint_thread, name="slack-no-events-hint", daemon=True).start()
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()  # blocks


if __name__ == "__main__":
    main()

