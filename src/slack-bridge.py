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
    $CLAUDE_CONFIG_DIR/channels/slack/access.json
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
import uuid
import re
import secrets
import sys
import threading
import time
import urllib.request
from pathlib import Path

# startup.sh redirects stdout to a log file, which makes CPython block-buffer
# it — diagnostic prints without flush=True sit invisible in the buffer, and
# SIGTERM kills the process without flushing, losing them entirely. Unlike
# discord-bridge, this bridge isn't even launched with PYTHONUNBUFFERED=1 —
# line-buffer structurally so every print lands in the log as it happens.
# Same fix as telegram-bridge (#1926).
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_priority import default_priority_for_source  # noqa: E402

# Observability: emit channel.slack.<in|out> into the local obs spine
# (src/observability). Guarded so a missing module never crashes the bridge.
try:
    from observability.channel import emit_channel as _emit_channel  # noqa: E402
except Exception:  # pragma: no cover — best-effort telemetry
    def _emit_channel(*_a, **_k):  # type: ignore
        return None
from result_markers import parse_markers  # noqa: E402
from message_chunking import chunk_message  # noqa: E402  (Result Router S3 — shared fence-aware chunker)
import local_task_protocol  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402
from util_paths import channel_access_path, claude_home_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from task_archive import find_task_file  # noqa: E402
from single_instance import acquire as _single_instance_acquire  # noqa: E402
from vault_intercept import intercept_vault_commands, redact_vault_commands  # noqa: E402
from chat_secret_filter import filter_chat_secrets, secret_handling_instruction  # noqa: E402
from slack_owner import resolve_proactive_owner_id  # noqa: E402
from slack_proactive_receipts import mark_delivered as mark_proactive_delivered  # noqa: E402
from slack_proactive_receipts import was_delivered as proactive_was_delivered  # noqa: E402

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
# Fall back to the channel .env when the tokens aren't already in our env. The
# Electron backend-supervisor gates the bridge on this file's presence but builds
# the child env from process.env + workspace .env only — it relies on each bridge
# self-loading its channel .env (discord/telegram already do). Without this, the
# supervisor-spawned bridge crash-loops on "not set". Mirrors discord-bridge.py.
# Tighten perms whenever the token file exists — even when the tokens are already
# in process env — so a world-readable .env never survives startup.
channels_env = claude_home_path("channels", "slack", ".env")
if channels_env.exists():
    try:
        os.chmod(channels_env, 0o600)  # token file — enforce owner-only, mirrors access.json treatment
    except OSError as e:
        # Best-effort hardening: a read-only volume, wrong ownership after a
        # restore/sync, or an ACL-restricted file must NOT crash the bridge at
        # startup — the file may still be perfectly readable. Warn and continue.
        print(f"  [startup] warning: could not chmod 0600 {channels_env}: {e}", flush=True)
if (not BOT_TOKEN or not APP_TOKEN) and channels_env.exists():
    for line in channels_env.read_text().splitlines():
        if line.startswith("SLACK_BOT_TOKEN=") and not BOT_TOKEN:
            BOT_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("SLACK_APP_TOKEN=") and not APP_TOKEN:
            APP_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
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


def write_owner_activity(channel: str, summary: str, channel_id=None) -> None:
    """Record owner activity — same schema as src/discord-bridge.py."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(time.time()),
            "channel": channel,
            "summary": summary[:80],
        }
        if channel_id:
            payload["channel_id"] = str(channel_id)
        # Per-PID staging name: this file is written by four processes (this
        # bridge + discord/telegram/sparrow). A shared ".json.tmp" name lets two
        # concurrent writers truncate and interleave the same temp file, so the
        # rename can publish torn JSON. A per-PID temp is never shared, and
        # os.replace is an atomic overwrite — last writer wins, cleanly. (#2222)
        tmp = OWNER_ACTIVITY_FILE.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, OWNER_ACTIVITY_FILE)
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


ACCESS_FILE = channel_access_path("slack")

# TOFU enrollment code — set at startup when access.json doesn't exist.
# The first DM must include this code to become owner. RETAINED for the whole
# process lifetime (never cleared) so the gate stays armed if access.json is
# deleted externally later (#899). None only when access.json already existed
# at startup (bridge already enrolled). See telegram-bridge.py for the same
# mechanism.
_TOFU_ENROLLMENT_CODE: str | None = None

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
# This map must survive bridge restarts: task/result files are durable, and a
# restarted bridge still needs the original channel + thread timestamp to route
# a late result. Discord already persists the equivalent map for this reason.
PENDING_REPLIES_FILE = STATE_DIR / "slack-pending-replies.json"


def _atomic_write_pending_replies(data: dict) -> None:
    """Persist reply routing without exposing a truncated JSON file on crash."""
    try:
        PENDING_REPLIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_REPLIES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(PENDING_REPLIES_FILE)
    except Exception as e:
        print(f"  [recovery] could not persist Slack pending replies: {e}", flush=True)


def load_pending_replies_from_disk() -> dict:
    """Restore reply routing on startup, aging out entries older than 7 days."""
    try:
        if not PENDING_REPLIES_FILE.exists():
            return {}
        data = json.loads(PENDING_REPLIES_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        now_ms = int(time.time() * 1000)
        max_age_ms = 7 * 86400 * 1000
        aged_out = []
        for task_id, info in list(data.items()):
            if not isinstance(info, dict) or not info.get("channel"):
                del data[task_id]
                continue
            try:
                ts_ms = int(task_id.split("-")[1])
                if now_ms - ts_ms > max_age_ms:
                    aged_out.append(task_id)
                    del data[task_id]
            except (ValueError, IndexError):
                pass
        if aged_out:
            print(f"  [recovery] aged out {len(aged_out)} Slack pending replies > 7d", flush=True)
        _atomic_write_pending_replies(data)
        return data
    except Exception as e:
        print(f"  [recovery] could not load Slack pending replies: {e}", flush=True)
        return {}


# Keyed by task_id; value is {channel, thread_ts, submitted_at, timed_out,
# access_tier}. Loading happens before the watcher starts, so any result that
# landed while the bridge was down is delivered on its first poll.
pending_replies: dict[str, dict] = load_pending_replies_from_disk()
pending_replies_lock = threading.Lock()


def _set_pending_reply(task_id: str, info: dict) -> None:
    with pending_replies_lock:
        pending_replies[task_id] = info
        _atomic_write_pending_replies(dict(pending_replies))


def _pop_pending_reply(task_id: str):
    with pending_replies_lock:
        target = pending_replies.pop(task_id, None)
        _atomic_write_pending_replies(dict(pending_replies))
    return target


def _mark_pending_timed_out(task_id: str) -> None:
    with pending_replies_lock:
        entry = pending_replies.get(task_id)
        if entry is None:
            return
        entry["timed_out"] = True
        _atomic_write_pending_replies(dict(pending_replies))


def _write_routed_task(task_file: Path, content: str, task_id: str, info: dict) -> None:
    """Persist the Slack route before exposing its task file to the core."""
    _set_pending_reply(task_id, info)
    try:
        task_file.write_text(content)
    except Exception:
        _pop_pending_reply(task_id)
        raise

# Per-task timeout. Mirrors task-bridge.ts's DEFAULT_TASK_TIMEOUT_MS (10 min):
# if the core session wedges (e.g. hits the 1M-context usage-credit gate and
# loops on the API error), no result file is ever written and the Slack user
# gets silence. After this many seconds we post a one-time "still working /
# may have hit a limit" reply so the failure is visible instead of silent.
# The pending entry is KEPT after notifying, so if the core later recovers and
# writes a result, the real answer still gets delivered. 0 disables.
TASK_TIMEOUT_SEC = int(os.environ.get("SLACK_TASK_TIMEOUT_SEC", "600"))

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            # When the bot token lacks the files:read scope (or the file is
            # otherwise unauthorized), Slack does NOT error — it 200s with an
            # HTML sign-in page. Persisting that page as e.g. a ".m4a" silently
            # corrupts the attachment: downstream transcription/parsing then
            # chokes on a login page. Detect it and fail cleanly instead so the
            # caller surfaces "no attachment" rather than feeding garbage on.
            ctype = (resp.headers.get("Content-Type") or "").lower()
            looks_html = ctype.startswith("text/html") or data[:64].lstrip()[:14].lower() == b"<!doctype html"
            if looks_html:
                print(
                    f"  [file] download for {name_hint} returned an HTML page, "
                    f"not the file — the Slack bot token is almost certainly "
                    f"missing the 'files:read' scope (add it at api.slack.com/apps "
                    f"→ OAuth & Permissions → Bot Token Scopes, then Reinstall).",
                    flush=True,
                )
                return None
        with open(local_path, "wb") as f:
            f.write(data)
        return str(local_path)
    except Exception as e:
        print(f"  [file] download failed for {name_hint}: {e}", flush=True)
        return None


def _ref_from_slack_file(file_dict: dict, local_path: str) -> "local_task_protocol.AttachmentRef":
    """Build an AttachmentRef from a Slack file object + its saved local path
    (interaction-model 4D, step 1.5). Reads Slack's `mimetype`/`name`/`size`
    defensively; falls back to the saved basename when `name` is absent. Pure —
    kept separate from the async handler so the field-reading is testable."""
    return local_task_protocol.AttachmentRef(
        locator=local_path,
        mime=(file_dict.get("mimetype", "") or ""),
        filename=(file_dict.get("name", "") or os.path.basename(local_path)),
        size=(file_dict.get("size", 0) or 0),
    )


def _transcribe_via_skill(local_path: str) -> str | None:
    """Call skills/audio-transcribe/scripts/transcribe.py. Returns transcript or None.

    The skill is optional — if it is absent the bridge falls back to the plain
    [File attached:] line unchanged. Any error from the subprocess is swallowed;
    transcription failure must never block task delivery.
    """
    import subprocess
    skill_script = Path(os.path.realpath(__file__)).parent.parent / "skills" / "audio-transcribe" / "scripts" / "transcribe.py"
    if not skill_script.exists():
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(skill_script), local_path],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception as e:
        print(f"  [stt] skill call failed for {os.path.basename(local_path)}: {e}", flush=True)
    return None


# When a sender ADDRESSES the bot (a DM, or an @mention — every _write_task call
# is one of those, per handle_mention/handle_message) but isn't on the allowlist,
# the access gate below drops the message. Historically that drop was silent, so
# the sender never knew their message wasn't received (owner ask 2026-07-15).
# Ack once, rate-limited per sender, before dropping. Mirrors discord-bridge.py.
_NOT_ALLOWLISTED_ACK_COOLDOWN_S = 3600
_not_allowlisted_ack_at: dict[str, float] = {}
_NOT_ALLOWLISTED_ACK_TEXT = (
    "👋 I got your message, but you're not on this Sutando's allowlist yet, so I "
    "can't act on it. Ask the owner to add you. _(automated notice)_"
)


def _ack_not_allowlisted(event: dict, user_id: str) -> None:
    """One-line 'not on the allowlist' reply so an addressed-but-dropped Slack
    message isn't silent. Rate-limited per sender (in-memory; resets on restart)."""
    now = time.time()
    if now - _not_allowlisted_ack_at.get(user_id, 0.0) < _NOT_ALLOWLISTED_ACK_COOLDOWN_S:
        return  # already acked this sender recently — don't spam / echo
    _not_allowlisted_ack_at[user_id] = now
    channel = event.get("channel", "")
    # in-thread for a channel @mention, top-level for a DM (mirrors _write_task)
    thread_ts = None if event.get("channel_type") == "im" else (event.get("thread_ts") or event.get("ts"))
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_NOT_ALLOWLISTED_ACK_TEXT)
        print(f"  [not-allowlisted-ack] sent to {user_id}", flush=True)
    except Exception as e:
        print(f"  [not-allowlisted-ack] send failed: {e}", flush=True)


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
    global _TOFU_ENROLLMENT_CODE
    allowed = load_allowed()
    if allowed is None:
        # TOFU state — require enrollment code before auto-onboarding as owner,
        # so an attacker who can DM the bot can't claim ownership first.
        # Enrollment is DM-only: channel @mentions also route here but carry no
        # channel_type=="im", so drop them — a leaked code must not be claimable
        # from a shared channel.
        if event.get("channel_type") != "im":
            print(f"  TOFU: ignored non-DM event from {user_id} — enrollment is DM-only", flush=True)
            return None
        channel = event.get("channel") or user_id
        if _TOFU_ENROLLMENT_CODE and _TOFU_ENROLLMENT_CODE not in (text or ""):
            try:
                app.client.chat_postMessage(
                    channel=channel,
                    text=(
                        "Enrollment code required.\n"
                        "Check the bridge startup log for your code and send it here."
                    ),
                )
            except Exception:
                pass
            print(f"  TOFU: rejected enrollment from {user_id} — code not presented", flush=True)
            return None
        allowed = tofu_onboard(user_id, username)
        # Keep _TOFU_ENROLLMENT_CODE valid for the process lifetime (do NOT clear
        # it) so the gate stays armed if access.json is deleted externally later
        # (#899), instead of falling through to an unguarded tofu_onboard().
    # Every _write_task call is a DM (handle_message) or an @mention
    # (handle_mention), so a non-allowlisted sender here DID address the bot —
    # ack them (rate-limited) before the fail-closed drop so it isn't silent.
    if user_id not in allowed:
        print(f"  Dropped message from non-allowed user {user_id}", flush=True)
        _ack_not_allowlisted(event, user_id)
        return None

    # Download any attached files BEFORE writing the task, so the task body
    # carries the local paths. Skips silently on failure — task still goes
    # through with whatever files did download.
    attachment_lines = []
    # Structured refs (interaction-model 4D, step 1.5) — accumulated alongside
    # the legacy [File attached:] body line (dual-write, additive).
    attachment_refs: list = []  # pragma: no cover
    for file_dict in event.get("files") or []:
        local_path = _download_slack_file(file_dict)
        if local_path:
            attachment_refs.append(_ref_from_slack_file(file_dict, local_path))  # pragma: no cover
            transcript = _transcribe_via_skill(local_path)
            if transcript:
                attachment_lines.append(f"[Voice transcript: {transcript}]")
            else:
                attachment_lines.append(f"[File attached: {local_path}]")
    attachment_note = ("\n" + "\n".join(attachment_lines)) if attachment_lines else ""

    if not text and not attachment_note:
        return None

    # Owner-activity state is persisted before tier/vault handling below. Use a
    # redacted preview so an ordinary pasted token never lands in state JSON.
    initial_secret_filter = filter_chat_secrets(text)
    detected_secret_types = set(initial_secret_filter.secret_types)
    safe_attachment = filter_chat_secrets(attachment_note)
    detected_secret_types.update(safe_attachment.secret_types)
    write_owner_activity(
        "slack",
        initial_secret_filter.text or safe_attachment.text,
        channel_id=event.get("channel"),
    )

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

    # Intercept vault commands before any disk write — must happen AFTER
    # access_tier is resolved so untrusted senders cannot write to Keychain.
    # Owner-tier: secrets go to Keychain, task file gets [STORED-IN-KEYCHAIN].
    # Non-owner: patterns redacted, Keychain untouched.
    if text:
        if access_tier == "owner":
            vault_result = intercept_vault_commands(text)
            text = vault_result.text
            if vault_result.stored:
                print(f"  [vault] stored keys: {vault_result.stored}", flush=True)
            if vault_result.failed:
                print(f"  [vault] store failed (still redacted): {vault_result.failed}", flush=True)
        else:
            text = redact_vault_commands(text)

    # Generic chat-secret detection is deliberately AFTER explicit vault
    # interception: named `vault set` values still reach Keychain, while any
    # other pasted token is redacted before task/prompt persistence.
    filtered_text = filter_chat_secrets(text)
    text = filtered_text.text
    detected_secret_types.update(filtered_text.secret_types)
    attachment_note = safe_attachment.text
    secret_notice = secret_handling_instruction("Slack", detected_secret_types)

    # Prepend an in-band system instruction for non-owner tiers so the
    # core agent cannot accidentally process a downgraded task with full
    # capabilities. Mirrors the Discord bridge's tier-specific instruction
    # block (see discord-bridge.py around `===SUTANDO SYSTEM INSTRUCTIONS===`).
    # Kept short here because Slack's downgrade surface today is just
    # "delegate to sandboxed read-only agent" — no Slack-state-prefetch
    # path equivalent to Discord's `_prefetch_discord_state_refs`.
    # Confine the user-derived portion BEFORE the bridge appends its own
    # `===SUTANDO SYSTEM INSTRUCTIONS===` block below — so a forged field/fence
    # in the message can't escalate tier or inject instructions, while the
    # bridge's legitimate fence (added next) stays intact. See task_body_guard.
    user_task_text = confine_user_content(
        f"[{prefix} @{username or user_id}] {text}{attachment_note}"
    )
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

    # Inject skill instructions so the agent follows the notify-before-work and
    # transcription protocol even after conversation compaction wipes context.
    # Only injected for owner tasks when the referenced skills are installed.
    # Use claude_home_path() — honours $CLAUDE_CONFIG_DIR → $CLAUDE_HOME → ~/.claude
    # resolution order (inline os.environ.get misses the $CLAUDE_HOME fallback).
    # Behaviorally covered by tests/bridge-skill-path-resolution.test.py (CLAUDE_CONFIG_DIR resolution).
    _notify_py = claude_home_path("skills", "task-progress", "scripts", "notify.py")
    _transcribe_py = claude_home_path("skills", "audio-transcribe", "scripts", "transcribe.py")
    skill_hints = ""
    if access_tier == "owner" and (_notify_py.exists() or _transcribe_py.exists()):
        hints_lines = ["===SKILL INSTRUCTIONS (follow before any other action)==="]
        step = 1
        if _notify_py.exists():
            notify_cmd = (
                f"python3 {_notify_py}"
                f" --source slack --channel-id {channel}"
                f' --message "On it — back in a moment."'
            )
            hints_lines.append(f"{step}. NOTIFY FIRST: {notify_cmd}")
            step += 1
        if attachment_lines and _transcribe_py.exists():
            for ap in attachment_lines:
                attached_path = ap.replace("[File attached: ", "").rstrip("]")
                if _notify_py.exists():
                    hints_lines.append(
                        '   Update notify message to: --message "Got your voice message, give me a moment."'
                    )
                hints_lines.append(
                    f"{step}. TRANSCRIBE: python3 {_transcribe_py} '{attached_path}'"
                )
                step += 1
        hints_lines.append(f"{step}. Then process and write result to results/{task_id}.txt")
        skill_hints = "\n" + "\n".join(hints_lines) + "\n"

    # interaction-model 4D, step 1.5: structured media headers alongside the
    # legacy [File attached:] body line (dual-write). Real headers after `task:`,
    # so confine_user_content defangs a forged body copy while these authentic
    # ones pass through. Uses the shared local_task_protocol helper (slack is the
    # third bridge; discord/telegram fold onto it in a follow-up dedup).
    media_headers = local_task_protocol.media_attachment_headers(  # pragma: no cover
        attachment_refs, bool(text and text.strip()))
    pending_info = {
        "channel": channel,
        "thread_ts": thread_ts,
        "access_tier": access_tier,  # threaded to the outbound obs event
        "submitted_at": time.time(),
        "timed_out": False,
    }
    task_content = (
        f"id: {task_id}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"source: slack\n"
        f"interaction_type: message\n"
        f"{media_headers}"
        f"channel_id: {channel}\n"
        f"user_id: {user_id}\n"
        f"access_tier: {access_tier}\n"
        f"priority: {priority}\n"
        f"task: {user_task_text}\n"
        f"{skill_hints}"
        f"{secret_notice}"
    )
    # If the bridge dies immediately after creation, the next process can still
    # route the result. The helper rolls the route back if task writing fails.
    _write_routed_task(task_file, task_content, task_id, pending_info)

    global _event_count
    with _event_count_lock:
        _event_count += 1

    print(f"  Wrote {task_id} from {prefix} @{username}", flush=True)
    # Observability: one inbound accepted-message event.
    _emit_channel(
        "slack", "in",
        user_id=str(user_id or ""),
        channel_id=str(channel),
        access_tier=access_tier,
        data={
            "task_id": task_id,
            "is_dm": str(channel).startswith("D"),
            "is_thread": bool(thread_ts),
        },
    )
    # Anonymous, opt-out product telemetry: one bucketed event per accepted
    # task, tagged only with the inbound surface. No-op when opted out / no key;
    # never task content or ids. See src/telemetry.py + TELEMETRY.md.
    try:  # pragma: no cover — fire-and-forget glue; logic tested in tests/telemetry.test.py
        from telemetry import task_processed  # sibling module (src/ on sys.path)

        task_processed("slack")
    except Exception:  # pragma: no cover — telemetry must never break the bridge
        pass
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


_EMPTY_MENTION_CLARIFICATION = (
    "The user mentioned the bot without any task text, and no recoverable "
    "same-user message was found in the Slack conversation. Acknowledge the mention "
    "and ask briefly what they would like help with."
)

# The recoverable half of a split turn ("Run it now" then a bare "@Sutando") is
# near-simultaneous. Recovering a same-user message from arbitrarily far back in
# the 100-message page re-surfaces a stale instruction "as if fresh" (CR #2230,
# bassilkhilo-ag2). Bound recovery to a generous window that still covers a real
# split (seconds to a couple of minutes) while refusing an hours-old instruction.
_EMPTY_MENTION_RECOVERY_MAX_AGE_S = 600  # 10 minutes


def _resolve_mention_text(event: dict, stripped_text: str) -> tuple[str, bool]:
    """Recover an empty mention from the sender's immediately prior thread turn.

    Slack only emits ``app_mention`` for the second half of a split turn such as
    ``Run it now`` followed by ``@Sutando``.  Previously the empty mention was
    dropped by ``_write_task`` and the user saw silence.  Restrict recovery to
    the same thread and same sender so another participant's text can never be
    attributed to the mentioning user.  If recovery is unavailable, create a
    clarification task instead of silently returning.
    """
    if stripped_text:
        return stripped_text, False

    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    current_ts = event.get("ts")
    user_id = event.get("user")
    if channel and current_ts and user_id:
        try:
            if thread_ts:
                response = app.client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=100,
                )
            else:
                response = app.client.conversations_history(
                    channel=channel,
                    latest=current_ts,
                    inclusive=False,
                    limit=100,
                )
            messages = response.get("messages") or []
            # replies are oldest-first; channel history is newest-first.
            newest_first = reversed(messages) if thread_ts else messages
            for message in newest_first:
                if message.get("ts", "") >= current_ts:
                    continue
                # A bot reply is a conversation boundary: it means the sender's
                # prior turn was already answered. Stop here rather than skipping
                # past it — otherwise a bare `@Sutando` after "delete X" → bot
                # "done" would recover and re-run "delete X" (CR #2230, qingyun-wu).
                if message.get("bot_id"):
                    break
                # Do not jump across another human participant and attribute an
                # older instruction to the current sender.
                if message.get("user") != user_id:
                    break
                # Recency bound: a same-user message older than the window is not
                # the other half of a split turn — it is a stale instruction that
                # must not be resurfaced. Newest-first, so once one is too old the
                # rest are older still → stop. (CR #2230, bassilkhilo-ag2)
                try:
                    if float(current_ts) - float(message.get("ts", "0")) > _EMPTY_MENTION_RECOVERY_MAX_AGE_S:
                        break
                except (TypeError, ValueError):
                    # Unknown age must fail CLOSED. Recovered text becomes a live
                    # task, so an unparseable ts would let the recency bound be
                    # bypassed by the one input it cannot evaluate.
                    break
                candidate = (message.get("text") or "").strip()
                without_mentions = re.sub(
                    r"(?:^|\s)<@[A-Z0-9]+>(?=\s|$)",
                    " ",
                    candidate,
                ).strip()
                if without_mentions:
                    return candidate, True
                # A prior same-user message that is mention-only (empty once its
                # mentions are stripped) is an already-served bare @mention turn,
                # not a message to skip past. Continuing would walk to an OLDER
                # instruction already answered in that turn — bassil CR #2230
                # repro: "delete the prod database", bare @Sutando (served), then
                # a new bare @Sutando re-recovered and re-ran "delete...". Treat
                # the served mention as a boundary and stop.
                break
        except Exception as exc:
            print(f"  [empty-mention] thread context lookup failed: {exc}", flush=True)

    return _EMPTY_MENTION_CLARIFICATION, False


@app.event("app_mention")
def handle_mention(event, say):
    """Channel @mention → task file."""
    user_id = event.get("user")
    username = _resolve_username(user_id) if user_id else None
    raw = event.get("text", "")
    # Strip the leading <@BOTID> mention from the text body for cleanliness.
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", raw).strip()
    text, recovered = _resolve_mention_text(event, text)
    prefix = "Slack mention (recovered prior message)" if recovered else "Slack mention"
    _write_task(event, prefix, text, username)


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


def _send_reply(channel: str, thread_ts: str | None, text: str, task_id: str | None = None, access_tier: str = "unknown") -> None:
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
    redirected = False
    for action in parsed.actions:
        if action.kind == "redirect":
            channel = action.value
            thread_ts = None
            redirected = True
            break

    file_paths = [a.value for a in parsed.actions if a.kind == "attach"]

    # Track real delivery: the Slack helpers swallow API errors, so the
    # outbound obs event must consult these rather than assume success.
    delivered_ok = True
    sent_files = 0

    # Post the text body in <=4000-char chunks (Slack's per-message limit is
    # 40k chars but readability suffers above ~4k). Use the shared fence-aware
    # chunker (Result Router S3) instead of a naive byte-slice: Slack posts
    # default to mrkdwn, so slicing mid-``` split a code block across two
    # messages and broke the rendering. chunk_message closes+reopens the fence
    # at each boundary so every chunk renders as a well-formed block.
    if clean_text:
        all_chunks_sent = True
        for chunk in chunk_message(clean_text, 4000):
            kwargs = {"channel": channel, "text": chunk}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                app.client.chat_postMessage(**kwargs)
            except Exception as e:
                print(f"[Slack] chat_postMessage failed: {e}", flush=True)
                all_chunks_sent = False
                break
        if not all_chunks_sent:
            delivered_ok = False
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
                sent_files += 1
                print(f"  Sent file: {fpath}", flush=True)
            else:
                delivered_ok = False
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

    # Observability: one delivered-reply event. outcome reflects whether the
    # text chunks + file uploads actually succeeded (the helpers swallow API
    # errors); file_count counts files actually delivered, not just intended.
    if clean_text or file_paths:
        _emit_channel(
            "slack", "out",
            channel_id=str(channel),
            access_tier=access_tier,
            outcome="ok" if delivered_ok else "error",
            data={
                "task_id": task_id,
                "is_dm": str(channel).startswith("D"),
                "is_thread": bool(thread_ts),
                "file_count": sent_files,
            },
        )

    # §7 audit ledger (Result Router S5): one line per resolved delivery so
    # "did the user ever see this?" is answerable without grepping bridge logs.
    # Guarded + never-raising — auditing must not block or crash delivery.
    if clean_text or file_paths:
        try:
            import result_audit
            result_audit.record(
                task_id or "",
                "failed" if not delivered_ok else ("redirected" if redirected else "delivered"),
                "slack",
            )
        except Exception:  # pragma: no cover  (defensive: result_audit import is safe + record() never raises)
            pass


def _record_skip_audit(task_id: str, skip_value: str) -> None:
    """Record §7 audit disposition for a skip-marked result (no_send / deduped)."""
    try:
        import result_audit as _ra
        _disp = "deduped" if skip_value == "deduped" else "no_send"
        _ra.record(task_id or "", _disp, "slack")
    except Exception:  # pragma: no cover  (defensive: record() never raises in practice)
        pass


def _check_task_timeouts() -> None:
    """Post a one-time reply for tasks the core never answered in time.

    Without this, a wedged core session (e.g. stuck looping on the
    1M-context usage-credit API error) leaves the Slack task orphaned in
    pending_replies forever — the user just sees silence. We mark the entry
    `timed_out` (so we notify at most once) but DO NOT pop it: if the core
    later recovers and writes results/<task_id>.txt, the normal reply path
    still delivers the real answer.
    """
    if TASK_TIMEOUT_SEC <= 0:
        return
    now = time.time()
    to_notify = []
    with pending_replies_lock:
        for task_id, info in pending_replies.items():
            if info.get("timed_out"):
                continue
            if now - info.get("submitted_at", now) > TASK_TIMEOUT_SEC:
                # Collect only — do NOT set timed_out here. Marking before the
                # send means a single Slack API hiccup (which raises below and
                # is merely logged) leaves the flag True forever, so the next
                # pass's `if info.get("timed_out"): continue` skips it and the
                # user never sees the warning — recreating the exact silent
                # no-op this watchdog exists to prevent. Mark only AFTER a
                # successful send. (Per @sonichi PR #1428 review, blocker 1.)
                to_notify.append((task_id, info["channel"], info.get("thread_ts")))
    if not to_notify:
        return
    mins = TASK_TIMEOUT_SEC // 60
    msg = (
        f":hourglass_flowing_sand: Still working on this — it's been over "
        f"{mins} min with no result. The core session may have hit a context "
        f"or usage-credit limit (check the Sutando CLI / `/usage-credits`). "
        f"I'll still post the answer here if it finishes."
    )
    for task_id, channel, thread_ts in to_notify:
        try:
            _send_reply(channel, thread_ts, msg, task_id=task_id)
        except Exception as e:
            # Send failed — leave timed_out unset so the next pass retries.
            print(f"[Slack] timeout notify failed for {task_id}: {e}", flush=True)
            continue
        # Notified once, successfully. Mark so we don't repeat. The entry may
        # have been popped by result_watcher if a real result landed meanwhile
        # — guard with get() so we don't resurrect a delivered task.
        _mark_pending_timed_out(task_id)
        print(f"  [timeout] notified Slack for {task_id} after {TASK_TIMEOUT_SEC}s", flush=True)


def result_watcher():
    """Background thread: polls results/ for replies + proactive messages."""
    heartbeat_file = REPO / "state" / "slack-bridge.heartbeat"
    last_heartbeat = 0.0
    while True:
        try:
            # Surface tasks the core never answered (timeout → visible reply).
            _check_task_timeouts()

            # Replies to pending tasks
            with pending_replies_lock:
                pending_ids = list(pending_replies.keys())
            for task_id in pending_ids:
                result_file = RESULTS_DIR / f"{task_id}.txt"
                if not result_file.exists():
                    continue
                reply_text = result_file.read_text().strip()
                if not reply_text:
                    continue
                with pending_replies_lock:
                    target = pending_replies.get(task_id)
                if not target:
                    continue

                # Skip-marker check via unified parser (#873). Equivalent to
                # the prior startswith trio but routed through one source of
                # truth so future skip markers added in result_markers.py
                # automatically apply here.
                _skip_parsed = parse_markers(reply_text)
                _skip_action = next((a for a in _skip_parsed.actions if a.kind == "skip"), None)
                if _skip_action is not None:
                    print(f"  Skipped (marker): {task_id}", flush=True)
                    # §7 audit ledger: skip-marked results are resolved deliveries
                    # (no_send / deduped), not silent voids. One line per result.
                    _record_skip_audit(task_id, _skip_action.value)
                else:
                    try:
                        _send_reply(target["channel"], target.get("thread_ts"), reply_text, task_id=task_id, access_tier=target.get("access_tier", "unknown"))
                        print(f"  Replied to {target['channel']}: {reply_text[:80]}...", flush=True)
                    except Exception as e:
                        print(f"[Slack] reply error: {e}", flush=True)
                        # Keep both the durable route and result file so the
                        # next poll (or restarted bridge) can retry delivery.
                        continue  # pragma: no cover - watcher loop retry; helper state is unit-tested

                _pop_pending_reply(task_id)
                archive_file(result_file, "results", task_id)
                archive_file(find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt", "tasks", task_id)

            # Proactive messages (sent to owner DM)
            if not presenter_mode_active():
                for f in list(RESULTS_DIR.iterdir()):
                    if not (f.name.startswith("proactive-") and f.suffix == ".txt"):
                        continue
                    delivery_id = f.name
                    # A producer may recreate the same deterministic result
                    # filename after the watcher successfully sends and removes
                    # it. Keep a durable receipt so that file-existence checks or
                    # retries cannot turn one schedule fire into duplicate DMs.
                    if proactive_was_delivered(STATE_DIR, delivery_id):
                        print(f"  [proactive] duplicate suppressed: {delivery_id}", flush=True)
                        _record_skip_audit(delivery_id, "deduped")
                        f.unlink(missing_ok=True)
                        continue
                    # Peek before claiming: skip Discord-targeted proactive files.
                    # [channel: <17-20 digit snowflake>] is a Discord-only marker;
                    # claiming it here dumps the literal text to Slack DM instead.
                    # Leave it for discord-bridge to claim. (#1401)
                    try:
                        peek = f.read_text(errors="ignore").lstrip()
                    except OSError:
                        continue
                    if peek.startswith("[channel:") and \
                            re.match(r'\[channel:\s*\d{17,20}\]', peek):
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
                    try:
                        access_data = json.loads(ACCESS_FILE.read_text())
                    except Exception:
                        access_data = {}
                    owner_id = resolve_proactive_owner_id(access_data)
                    if owner_id is not None:
                        # Open a DM channel to the owner (idempotent).
                        try:
                            resp = app.client.conversations_open(users=owner_id)
                            dm_channel = resp["channel"]["id"]
                            _send_reply(dm_channel, None, text, access_tier="owner")  # proactive → owner
                            mark_proactive_delivered(STATE_DIR, delivery_id)
                            print(f"  [proactive] sent to {owner_id}: {text[:80]}", flush=True)
                        except Exception as e:
                            print(f"  [proactive] failed: {e}", flush=True)
                    else:
                        print(f"  [proactive] no owner in allowFrom, skipping {claim.name}", flush=True)
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

def main():  # pragma: no cover
    global _TOFU_ENROLLMENT_CODE
    _single_instance_acquire("slack-bridge")
    print("Slack bridge started. Socket Mode connecting...", flush=True)
    _recover_orphan_sending_files()
    # Prime the in-memory access cache so tofu_onboard() can detect external
    # deletions even on the very first inbound message after a restart (#899).
    load_allowed()

    # TOFU enrollment code: generated when access.json doesn't exist so
    # the first DM must present it before being auto-enrolled as owner.
    if not ACCESS_FILE.exists():
        _TOFU_ENROLLMENT_CODE = secrets.token_hex(3)  # 6-char hex, 16M combinations
        print("", flush=True)
        print("  *** TOFU enrollment required ***", flush=True)
        print(f"  Enrollment code: {_TOFU_ENROLLMENT_CODE}", flush=True)
        print("  Send this code in your first DM to register as owner.", flush=True)
        print("  Anyone who sends this code first becomes owner — keep it private.", flush=True)
        print("", flush=True)

    threading.Thread(target=result_watcher, name="slack-result-watcher", daemon=True).start()
    threading.Thread(target=_no_events_hint_thread, name="slack-no-events-hint", daemon=True).start()
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()  # blocks


if __name__ == "__main__":
    main()
