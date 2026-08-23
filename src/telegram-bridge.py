#!/usr/bin/env python3
"""
Telegram bridge for Sutando — polls bot messages, writes to tasks/, sends replies from results/.
Works alongside the voice task bridge. Runs as a background daemon.

Usage: python3 src/telegram-bridge.py
"""

# startup.sh launches this via bare `python3`, which is /usr/bin/python3 (3.9)
# on stock macOS. PEP-604 unions (`str | None`) are evaluated at def-time on
# 3.9 and raise TypeError, crashing the bridge on import. Lazy annotations
# (PEP 563) make every annotation in this file a string — never evaluated —
# so 3.9 is safe. Must precede all other imports.
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# startup.sh redirects stdout to a log file, which makes CPython block-buffer
# it — diagnostic prints (e.g. api()'s "API error ..." lines) sit invisible in
# the buffer for hours, and SIGTERM kills the process without flushing, losing
# them entirely. Both silent-wedge incidents (2026-06-15 TLS, 2026-07-05 stale
# heartbeat) had empty logs for exactly this reason. Line-buffer so every
# print lands in the log as it happens.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Vision-frame helper — pushes the latest photo into the active voice session
# so Gemini can react in-stream. No-op when voice isn't connected. Import is
# best-effort so the bridge keeps booting if vision_push.py is missing.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from vision_push import push_image as _push_vision_image  # type: ignore
except Exception:  # pragma: no cover — bridge must keep running
    def _push_vision_image(path: str, source: str = "telegram") -> bool:  # type: ignore
        return False
from task_priority import default_priority_for_source  # noqa: E402
from optional_script import run_optional_script as _run_optional_script_shared  # noqa: E402
from proactive_recovery import (claim_for_delivery, recover_orphan_sending_files,  # noqa: E402
                                release_claim)
from owner_activity import write_owner_activity as _write_owner_activity_shared  # noqa: E402

# Observability: emit channel.telegram.<in|out> into the local obs spine
# (src/observability). Guarded so a missing module never crashes the bridge.
try:
    from observability.channel import emit_channel as _emit_channel  # noqa: E402
except Exception:  # pragma: no cover — best-effort telemetry
    def _emit_channel(*_a, **_k):  # type: ignore
        return None
import local_task_protocol  # noqa: E402
from result_markers import parse_markers
from message_chunking import chunk_plain_text  # plain transport: byte-identical chunking  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402
from dedup_recovery import plan_dedup_recovery, report_disposition  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402
from util_paths import channel_access_path, claude_home_path, write_private_text  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402
from presenter_mode import presenter_mode_active  # noqa: E402
from sutando_config import config_get, config_get_env_first  # noqa: E402
from task_archive import find_task_file  # noqa: E402
from task_archive import archive_file as _shared_archive_file  # noqa: E402
from single_instance import acquire as _single_instance_acquire  # noqa: E402
import progress_stream  # noqa: E402  (opt-in owner progress streaming, SUTANDO_PROGRESS_STREAM=1)
from vault_intercept import intercept_vault_commands, redact_vault_commands  # noqa: E402
from chat_secret_filter import filter_chat_secrets, secret_handling_instruction  # noqa: E402
REPO = resolve_workspace()
TASKS_DIR = REPO / "tasks"
RESULTS_DIR = REPO / "results"

# Allowlist for paths that may be sent via Telegram [file: /path] markers.
# Mirrors _is_path_sendable() in discord-bridge.py.
# Outbound attachment allowlisting is canonical policy — src/send_allowlist.py
# is the single source of truth. A hand-written copy here drifted from it: it
# lacked the personal-notes, iclr-backups and launch-assets roots, so a file
# Discord would happily send was silently dropped from a Telegram reply.
# Telegram inherits the complete canonical policy with no extra roots.
from policy.egress.attachment import is_path_sendable as _is_path_sendable  # noqa: E402


# --- Config loading (independent of _is_path_sendable above) ---

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass  # python-dotenv not installed — token loaded from channels config below

# Also load from channels config — config file wins over stale shell env.
# `setdefault` previously let a stale TELEGRAM_BOT_TOKEN from a prior shell
# session silently override the freshly-rotated value, same bug class as
# skills/x-twitter/x-post.py (see PR #416 commit message for full context).
channels_env = claude_home_path("channels", "telegram", ".env")
if channels_env.exists():  # pragma: no cover — telegram import path not driven by the perms test
    try:
        os.chmod(channels_env, 0o600)  # token file — enforce owner-only, mirrors access.json treatment
    except OSError as e:
        # Best-effort hardening: a read-only volume, wrong ownership after a
        # restore/sync, or an ACL-restricted file must NOT crash the bridge at
        # startup — the file may still be perfectly readable. Warn and continue.
        print(f"  [startup] warning: could not chmod 0600 {channels_env}: {e}", flush=True)
    for line in channels_env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip()
            # Strip matching surrounding quotes — mirrors python-dotenv.
            # Without this, `TELEGRAM_BOT_TOKEN="abc"` in .env stores
            # the literal `"abc"` (with quotes) in os.environ; the
            # Telegram REST URL becomes
            # `https://api.telegram.org/bot"abc"/getUpdates` and Telegram
            # returns 404. Quoted .env values are a common convention.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            os.environ[k.strip()] = v

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    # Last resort: the Keychain vault. A peer host lost this token for eight
    # weeks because the only copy lived in a running process's environment and
    # `vault set` was not read by anything.
    from channel_token import token_from_vault  # noqa: E402
    TOKEN = token_from_vault("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    print("TELEGRAM_BOT_TOKEN not set in channels/telegram/.env and not in the "
          "vault (`vault set TELEGRAM_BOT_TOKEN`)")
    exit(1)

TASKS_DIR = REPO / "tasks"
RESULTS_DIR = REPO / "results"
STATE_DIR = REPO / "state"
ARCHIVE_TASKS_DIR = REPO / "tasks" / "archive"
ARCHIVE_RESULTS_DIR = REPO / "results" / "archive"
OWNER_ACTIVITY_FILE = STATE_DIR / "last-owner-activity.json"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def extract_forward_note(msg: dict) -> str:
    """Return a ` [forwarded from ...]` suffix for a Telegram message dict.

    Handles Bot API 7.0+ `forward_origin` (user / hidden_user / chat / channel)
    and legacy `forward_from` / `forward_sender_name`. Returns "" for
    non-forwarded messages or unknown `forward_origin.type` values so the
    bridge fails open rather than crashing on future Telegram additions.
    """
    fwd_origin = msg.get("forward_origin") or {}
    if fwd_origin:
        fwd_type = fwd_origin.get("type")
        if fwd_type == "user":
            u = fwd_origin.get("sender_user", {})
            name = u.get("username") or u.get("first_name") or "unknown"
            return f" [forwarded from @{name}]"
        if fwd_type == "hidden_user":
            name = fwd_origin.get("sender_user_name", "hidden")
            return f" [forwarded from {name}]"
        if fwd_type == "chat":
            chat = fwd_origin.get("sender_chat", {})
            name = chat.get("title") or chat.get("username") or "channel"
            return f" [forwarded from chat: {name}]"
        if fwd_type == "channel":
            chat = fwd_origin.get("chat", {})
            name = chat.get("title") or chat.get("username") or "channel"
            return f" [forwarded from channel: {name}]"
        return ""
    if "forward_from" in msg:
        u = msg["forward_from"]
        name = u.get("username") or u.get("first_name") or "unknown"
        return f" [forwarded from @{name}]"
    if "forward_sender_name" in msg:
        return f" [forwarded from {msg['forward_sender_name']}]"
    return ""


def write_owner_activity(channel: str, summary: str, channel_id=None) -> None:
    """Record owner activity using the shared provider-neutral schema."""
    _write_owner_activity_shared(
        OWNER_ACTIVITY_FILE,
        channel,
        summary,
        channel_id,
        on_error=lambda exc: print(f"  [owner-activity] write failed: {exc}"),
    )


def archive_file(src: "Path", kind: str, task_id: str) -> bool:
    """Adapter: inject this bridge's archive roots + logger into the shared
    never-delete policy. It used to unlink the source when the move failed,
    which destroyed the only copy of the task."""
    return _shared_archive_file(
        src, kind, task_id,
        tasks_dir=ARCHIVE_TASKS_DIR, results_dir=ARCHIVE_RESULTS_DIR,
        log=lambda m: print(f"[Telegram]{m}"))


# Load access config
ACCESS_FILE = channel_access_path("telegram")

# TOFU enrollment code — set at startup when access.json doesn't exist.
# The first DM must include this code to become owner. RETAINED for the
# whole process lifetime (never cleared) so the gate stays armed if
# access.json is deleted externally later (#899). None only when
# access.json already existed at startup (bridge already enrolled).
_TOFU_ENROLLMENT_CODE: str | None = None

def load_allowed():
    """Return the set of allowed sender IDs, OR None if access.json doesn't exist.

    The None vs empty-set distinction matters for trust-on-first-use (TOFU)
    auto-onboarding: "file missing" → the bridge has never been configured,
    so the first DM should auto-onboard the sender as the owner. "File exists
    but empty allowFrom" → the admin explicitly locked it down; never TOFU.
    """
    try:
        data = json.loads(ACCESS_FILE.read_text())
        return set(data.get("allowFrom", []))
    except FileNotFoundError:
        return None
    except Exception:
        return set()


def _resolve_proactive_owner_id(env_override: str | None, access_data: dict) -> str | None:
    """Resolve the recipient for a proactive owner-notification.

    Priority order:
      1. ``$SUTANDO_DM_OWNER_ID`` env override.
      2. ``tierMap[uid] == "owner"`` — the unique tier-tagged owner from
         ``access.json``. Wins over `tofuOwner` because tier tags are an
         explicit admin signal.
      3. ``tofuOwner`` field — recorded by :func:`tofu_onboard` on first
         install. Telegram-specific. Only honored if `tofuOwner` is
         still present in ``allowFrom`` — admins who explicitly removed
         it have signaled they no longer want it treated as the owner.
      4. First entry in ``allowFrom`` IN LIST ORDER. The list-order
         convention is meaningful: admins put the human owner first.

    Returns ``None`` when ``allowFrom`` is empty.

    Pure function — no I/O — so it's unit-testable in isolation.
    """
    if env_override:
        return env_override
    allow_list = access_data.get("allowFrom") or []
    if not allow_list:
        return None
    tier_map = access_data.get("tierMap") or {}
    tier_owner = next(
        (uid for uid in allow_list if tier_map.get(uid) == "owner"),
        None,
    )
    if tier_owner is not None:
        return str(tier_owner)
    tofu_owner = access_data.get("tofuOwner")
    if tofu_owner is not None and tofu_owner in allow_list:
        return str(tofu_owner)
    return str(allow_list[0])


def tofu_onboard(sender_id, username):
    """First-time auto-onboard: write access.json with this sender as owner.

    Triggered when access.json doesn't exist (i.e., the bridge has never been
    configured for any user) AND a DM arrives. The expected flow is: user
    rotates a token, starts the bridge, sends "hi" to their own bot, and
    Sutando auto-trusts that first DM as coming from them. Subsequent senders
    will be rejected as non-allowed and need explicit `/telegram:access allow`.

    Logs the onboarding so the act is visible. Safe-by-default: if the file
    already exists at write time (race with manual config), we don't clobber.
    """
    if ACCESS_FILE.exists():  # race-safety: someone else wrote it first
        return load_allowed() or set()
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "allowFrom": [sender_id],
        "tofuOwner": sender_id,
        "tofuOnboardedAt": int(time.time()),
        "tofuOnboardedUsername": username or None,
    }
    write_private_text(ACCESS_FILE, json.dumps(payload, indent=2) + "\n")  # don't inherit umask 644 — file holds owner's Telegram user ID
    print(f"  TOFU: auto-onboarded @{username} (id={sender_id}) as owner — wrote {ACCESS_FILE}")
    return {sender_id}

def api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"API error {e.code}: {body}")
        return {"ok": False}

INBOX_DIR = REPO / "telegram-inbox"
INBOX_DIR.mkdir(exist_ok=True)

def _transcribe_via_skill(local_path: str) -> str | None:
    """Call skills/audio-transcribe/scripts/transcribe.py. Returns transcript or None.

    Optional — if the skill is absent the caller falls back to [Voice note attached:].
    Errors are swallowed; transcription failure must never block task delivery.
    """
    skill_script = Path(os.path.realpath(__file__)).parent.parent / "skills" / "audio-transcribe" / "scripts" / "transcribe.py"
    return _run_optional_script_shared(
        skill_script,
        [local_path],
        timeout=25,
        on_error=lambda exc: print(
            f"  [stt] skill call failed for {os.path.basename(local_path)}: {exc}",
            flush=True,
        ),
    )


def download_file(file_id, name_hint="file"):
    """Download a file from Telegram and save locally."""
    result = api("getFile", file_id=file_id)
    if not result.get("ok"):
        return None
    file_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    ext = os.path.splitext(file_path)[1] or os.path.splitext(name_hint)[1] or ""
    local_name = f"{int(time.time()*1000)}{ext}"
    local_path = INBOX_DIR / local_name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Sutando"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(local_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        return str(local_path)
    except Exception as e:
        print(f"  Download failed: {e}")
        return None

def send_file(chat_id, file_path, caption=""):
    """Send a file via Telegram multipart upload."""
    import mimetypes
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    boundary = f"----sutando{int(time.time()*1000)}"
    fname = os.path.basename(file_path)

    # Determine send method based on mime type
    if mime.startswith("image/"):
        method, field = "sendPhoto", "photo"
    else:
        method, field = "sendDocument", "document"

    with open(file_path, "rb") as f:
        file_data = f.read()

    body = b""
    # File part
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    body += file_data
    body += b"\r\n"
    # chat_id part
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode()
    # caption part
    if caption:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Send file failed: {e}")
        return {"ok": False}

def send_reply(chat_id, text, task_id: str | None = None) -> dict:
    """Send a reply's text + any [file:]-marked attachments.

    Returns a delivery summary ``{"text_chunks", "files_sent", "ok"}`` so the
    caller can emit ONE accurate channel.telegram.out event. api()/send_file()
    swallow HTTP errors (return {"ok": False} / None), so success can't be
    assumed — we consult their return values. The task-reply path passes a
    marker-stripped body and sends parsed.actions attachments itself, then folds
    those into the same event (so file-only replies still report a delivery and
    the count/outcome stay accurate).

    Marker grammar comes solely from ``result_markers.parse_markers`` — this
    function must never re-declare it. It previously compiled a local
    ``file|send|attach`` regex, which stripped attachment markers but left every
    OTHER marker in the body. The proactive path (``poll_proactive``) passes raw
    result text, so ``[dm-only]`` and ``[channel:]`` leaked verbatim into the
    owner's message — the morning briefing is emitted as a proactive result
    carrying ``[dm-only]``, so it rendered with the marker visible.

    Parsing here (rather than at each call site) mirrors slack-bridge's
    ``_send_reply`` and makes the function safe for both callers: parse_markers
    is idempotent on an already-stripped body, so the task path — which passes
    ``parsed.body`` and sends its own attachments — yields zero actions here and
    cannot double-send."""
    parsed = parse_markers(text)
    files = [a.value for a in parsed.actions if a.kind == "attach"]
    clean_text = parsed.body
    # Fence-aware chunks via the shared chunker — naive [i:i+4000] slicing tore
    # code fences at the boundary and re-declared policy discord/slack share.
    chunks = chunk_plain_text(clean_text, 4000) if clean_text else []
    text_chunks = len(chunks)
    delivered_ok = True
    files_sent = 0

    # Send text (if any remains after extracting file refs)
    if clean_text:
        for piece in chunks:
            resp = api("sendMessage", chat_id=chat_id, text=piece)
            if not (isinstance(resp, dict) and resp.get("ok")):
                delivered_ok = False
        try:
            import outbox_log
            outbox_log.append(
                channel_type="telegram",
                recipient=str(chat_id),
                body=clean_text,
                task_id=task_id,
            )
        except Exception:
            pass

    # Send files (allowlist-gated; see _is_path_sendable)
    for fpath in files:
        fpath = fpath.strip()
        if _is_path_sendable(fpath):
            resp = send_file(chat_id, fpath)
            if isinstance(resp, dict) and resp.get("ok"):
                files_sent += 1
                print(f"  Sent file: {fpath}", flush=True)
            else:
                delivered_ok = False
                print(f"  Send file failed: {fpath}", flush=True)
        elif os.path.isfile(fpath):
            api("sendMessage", chat_id=chat_id, text=f"(file access denied: {fpath})")
            print(f"  BLOCKED file: {fpath}")
        elif not fpath:
            # An EMPTY target is malformed, not a prose quotation — surface it,
            # or a file-only result is retired with zero user-visible output.
            api("sendMessage", chat_id=chat_id,
                text="(a file marker in this reply had no path — nothing attached)")
            print("  file marker with EMPTY path — malformed, surfaced", flush=True)
        else:
            # Prose-quoted `[file:/path]` substrings extract as markers
            # but reference no actual file. Don't ship the warning to
            # the user; log for operator visibility on real typos. Same
            # rationale as discord-bridge:poll_results.
            print(f"  file marker, file not found — likely a prose quotation: {fpath}", flush=True)

    return {"text_chunks": text_chunks, "files_sent": files_sent, "ok": delivered_ok}

def _recover_orphan_sending_files() -> int:
    """Recover this adapter's stranded proactive delivery claims."""
    return recover_orphan_sending_files(RESULTS_DIR)


# --- Opt-in owner progress streaming (Telegram parity with Discord PR #97) ---
# While a long OWNER task runs, post a `⏳ <step> (Ns)` placeholder and edit it
# in place from core-status.step, deleting it when the result lands. Reuses the
# pure policy in progress_stream.py (same thresholds/gates as the Discord bridge).
# OFF unless SUTANDO_PROGRESS_STREAM=1. Best-effort: any Telegram API error is
# swallowed so the real result still delivers.
_progress_msgs: dict = {}        # task_id -> {message_id, chat_id, first, last_edit, last_text} | {"expired": True}
pending_task_tiers: dict = {}    # task_id -> access_tier; in-memory ONLY → fail-closed on restart
pending_task_private: dict = {}  # task_id -> chat audience is private; in-memory ONLY → fail-closed on restart


def _render_progress_text(elapsed: float, task_id: str) -> str:
    """The allowlist holds a USER id but that user can write from a group, so the gate
    is the recorded chat audience; unknown (e.g. after a restart) means not-private."""
    if not progress_stream.step_visible_in(pending_task_private.get(task_id, False)):
        return progress_stream.format_progress(None, elapsed)
    step = progress_stream.current_step(progress_stream.read_core_status(STATE_DIR))
    return progress_stream.format_progress(step, elapsed)


def _dedup_recover(task_id: str, holder_id, chat_id) -> tuple[str | None, str]:
    """Route the shared dedup-recovery plan.

    Returns ``(new_task_id_to_route, disposition)`` where disposition is the
    shared "archive"/"retain" -- an undelivered report must not retire the ask.
    """
    action, delivered, requeued = "defer", None, None
    try:
        action, payload = plan_dedup_recovery(
            RESULTS_DIR, TASKS_DIR, task_id, holder_id, chat_id,
            f"task-{int(time.time() * 1000)}")
        if action == "requeue":
            print(f"  [dedup] re-queued {task_id} as {payload}", flush=True)
            requeued = payload
        elif action == "report":
            # send_reply already reports its own outcome; ignoring it archived
            # a failed report as though the asker had been told.
            delivered = bool((send_reply(chat_id, payload, task_id=task_id)
                              or {}).get("ok"))
            print(f"  [dedup] unresolved for {task_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the disposition, not the raise, decides
        print(f"  [dedup] recovery failed for {task_id}: {exc}", flush=True)
    return requeued, report_disposition(action, delivered)


def _clear_progress(task_id: str) -> None:
    """Delete a task's progress placeholder (if any) and drop its tracking.
    Called when the result is delivered/skipped/given-up so the placeholder
    doesn't linger next to the real reply."""
    pending_task_tiers.pop(task_id, None)
    pending_task_private.pop(task_id, None)
    info = _progress_msgs.pop(task_id, None)
    if info and info.get("message_id") and info.get("chat_id") is not None:
        try:
            api("deleteMessage", chat_id=info["chat_id"], message_id=info["message_id"])
        except Exception:
            pass


def _find_task_file_anywhere(tasks_dir: Path, task_id: str) -> Path | None:
    """Locate task_id's file across live tasks/ (bare or claimed-core-N) AND
    the archive (flat tasks/archive/, tasks/processed/, and month-partitioned
    tasks/archive/YYYY-MM/).

    find_task_file() alone only checks live tasks/ — measured on a real host
    (2026-07-20, per @qingyun-wu's review on this PR): of the result files
    still sitting undelivered in results/, 0 had their task file still in
    tasks/ and effectively all had it already in tasks/archive/. Something
    other than this bridge's own delivery path (which archives task+result
    together, but only AFTER a successful send) moves task files into the
    archive independently — most likely task-orphan-check running on a
    session restart, classifying the still-undelivered task as "done"
    because its result already exists. Recovery must therefore check the
    archive too, or it silently no-ops in the actual common case.
    """
    found = find_task_file(tasks_dir, task_id)
    if found:
        return found
    return local_task_protocol.find_archived_task(tasks_dir, task_id)


def _recover_orphaned_task_routing(results_dir: Path, tasks_dir: Path, known_task_ids: set) -> dict:
    """Rebuild {task_id: chat_id} for Telegram tasks whose result file exists
    but whose in-memory routing was lost — e.g. this process restarted
    mid-task (a result written by the OLD process's `pending_replies` entry
    has no counterpart in the NEW process's empty dict, so it sits in
    results/ forever, un-delivered and silently orphaned). chat_id is
    durable — it's in the task file's own headers from creation time — even
    though pending_replies is memory-only (mirrors the same bug class
    proposed for src/slack-bridge.py in #2218, open as of this PR).

    Only claims tasks THIS bridge actually wrote (source: telegram), so a
    crashed slack/discord bridge's own stranded results are left alone for
    them to recover, not silently swallowed here.
    """
    recovered = {}
    for result_file in results_dir.glob("task-*.txt"):
        task_id = result_file.stem
        if task_id in known_task_ids:
            continue
        task_file = _find_task_file_anywhere(tasks_dir, task_id)
        if not task_file:
            continue
        try:
            # Recovery runs right after the crash that tore these files, so a
            # strict read here exits main() — headers are at the top and survive.
            text = task_file.read_text(errors="replace")
        except OSError:
            continue
        headers = local_task_protocol.parse_task_headers(text).headers
        if headers.get("source") != "telegram":
            continue
        chat_id = headers.get("chat_id")
        if not chat_id:
            print(f"  [recovery] {task_id}: task file found but no chat_id header — skipping", flush=True)
            continue
        try:
            recovered[task_id] = int(chat_id)
        except ValueError:
            print(f"  [recovery] {task_id}: chat_id header {chat_id!r} isn't numeric — skipping", flush=True)
            continue
    return recovered


def _gather_pending_task_ids(pending_replies: dict, results_dir: Path, tasks_dir: Path) -> list:
    """Fold any orphaned-by-restart routing recovered from disk into
    `pending_replies` (mutated in place via setdefault — if the id
    reappeared through normal means in the meantime, that entry wins), and
    return the full task_id list for this poll.

    Must run every tick, not once at startup (per @qingyun-wu's review,
    correcting my earlier "run once" design): a task can be created by the
    OLD process, survive a restart, and have its result land AFTER the new
    process's startup scan — the core is still processing it at restart
    time. A one-time startup scan sees no result yet, the task_id is never
    registered (it wasn't created by *this* process), and nothing ever
    looks for it again once the scan has passed — the reply is dropped for
    the life of the process. Confirmed as the exact mechanism behind the
    26-file live-host measurement in the sibling review.

    This isn't the "full glob every tick" cost it looks like: the outer
    `results_dir.glob()` is a cheap directory listing, and
    `_recover_orphaned_task_routing()` only pays the expensive per-file cost
    (reading task-file headers, checking the archive) for task_ids NOT
    already in `pending_replies` — on a steady-state tick with no pending
    restart-orphans, that set is empty and the extra work is zero.
    """
    known_ids = set(pending_replies)
    recovered = _recover_orphaned_task_routing(results_dir, tasks_dir, known_ids)
    for tid, chat_id in recovered.items():
        pending_replies.setdefault(tid, chat_id)
        print(f"  [recovered] {tid} routing from task file (was orphaned after a restart)", flush=True)
    return list(pending_replies)


def poll_progress(pending_replies: dict) -> None:
    """One pass of the progress streamer; called once per main-loop tick.
    No-op unless SUTANDO_PROGRESS_STREAM=1."""
    if not progress_stream.stream_enabled():
        return
    now = time.time()
    for task_id, chat_id in list(pending_replies.items()):
        done = (RESULTS_DIR / f"{task_id}.txt").exists()
        info = _progress_msgs.get(task_id)
        if info is not None:
            if info.get("expired"):
                continue
            if done:
                # Result arrived → remove placeholder; real reply delivers via the loop below.
                _clear_progress(task_id)
                continue
            elapsed = now - info["first"]
            if progress_stream.placeholder_expired(elapsed):
                try:
                    api("deleteMessage", chat_id=chat_id, message_id=info["message_id"])
                except Exception:
                    pass
                _progress_msgs[task_id] = {"expired": True}  # terminal — never re-post
                continue
            if progress_stream.should_edit(now, info["last_edit"]):
                text = _render_progress_text(elapsed, task_id)
                if text != info.get("last_text"):
                    try:
                        api("editMessageText", chat_id=chat_id, message_id=info["message_id"], text=text)
                    except Exception:
                        pass
                    info["last_text"] = text
                info["last_edit"] = now
            continue
        # No placeholder yet for this task.
        if done:
            continue  # finished before the threshold — stay silent
        if task_id not in pending_task_tiers:
            continue  # tier unknown (e.g. post-restart recovery) → fail-closed, don't stream
        if not progress_stream.should_stream_task(pending_task_tiers.get(task_id)):
            continue  # non-owner tier → no placeholder
        try:
            created = int(task_id.split("-")[1]) / 1000.0
        except (ValueError, IndexError):
            created = now
        if progress_stream.should_post_placeholder(now - created):
            text = _render_progress_text(now - created, task_id)
            resp = api("sendMessage", chat_id=chat_id, text=text)
            mid = (resp or {}).get("result", {}).get("message_id")
            if mid:
                _progress_msgs[task_id] = {
                    "message_id": mid, "chat_id": chat_id,
                    "first": created, "last_edit": now, "last_text": text,
                }
            else:
                # Send failed (chat blocked, rate-limited, …). Mark terminal so we
                # don't re-hammer the API every tick for the rest of the task.
                _progress_msgs[task_id] = {"expired": True}
    # GC: drop tracking for tasks no longer pending (delivered through another path).
    # Sweep BOTH dicts independently — a fast task can have a tier but never a
    # placeholder, so keying GC only off _progress_msgs would leak its tier.
    for tid in list(_progress_msgs.keys()):
        if tid not in pending_replies:
            _progress_msgs.pop(tid, None)
    for tid in list(pending_task_tiers.keys()):
        if tid not in pending_replies:
            pending_task_tiers.pop(tid, None)
    for tid in list(pending_task_private.keys()):
        if tid not in pending_replies:
            pending_task_private.pop(tid, None)


def log_privacy_setting(get_me):
    """Report the bot-wide BotFather privacy setting at boot.

    Scope matters: this flag is GLOBAL, and an administrator bot receives every group
    message regardless of it — so the flag alone never determines what one group delivers.
    """
    try:
        me = (get_me() or {}).get("result") or {}
    except Exception as e:  # noqa: BLE001 — a diagnostic must never take the bridge down
        return f"[Telegram] privacy-setting: getMe failed ({e}) — setting unknown"
    if not me:
        return "[Telegram] privacy-setting: getMe returned no result — setting unknown"
    BOT_EXCEPTION = (
        "one exception applies regardless: Telegram never delivers a message sent by "
        "another bot, even to an administrator or with privacy mode off "
        "(https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)"
    )
    if me.get("can_read_all_group_messages"):
        return ("[Telegram] privacy-setting: privacy mode OFF (BotFather, bot-wide) — "
                f"all group messages from human senders are delivered to this bot; "
                f"{BOT_EXCEPTION}")
    return (
        "[Telegram] privacy-setting: privacy mode ON (BotFather, bot-wide). In a group where "
        f"@{me.get('username') or 'this bot'} is NOT an administrator it receives only "
        "commands addressed to it (/command@bot), replies to its own messages, messages sent "
        "via it inline, and general commands when it posted last — a plain @username mention "
        "is NOT delivered. Where it IS a group administrator it receives everything from human "
        f"senders regardless of this setting, so this flag alone does not describe any "
        f"particular group's reach; {BOT_EXCEPTION}"
    )


def main():  # pragma: no cover
    global _TOFU_ENROLLMENT_CODE
    _single_instance_acquire("telegram-bridge")
    print("Telegram bridge started. Polling for messages...", flush=True)
    print(log_privacy_setting(lambda: api("getMe")), flush=True)
    # Restart-safety: sweep orphan `.sending` files before the poll
    # loop starts. See _recover_orphan_sending_files for rationale.
    _recover_orphan_sending_files()

    # TOFU enrollment code: generated when access.json doesn't exist so
    # the first DM must present it before being auto-enrolled as owner.
    # Prevents an attacker who can DM the bot from grabbing ownership before
    # the legitimate owner does. Stays valid for the whole process lifetime
    # (never cleared after enrollment) so the gate remains armed if access.json
    # is deleted externally later (#899); only a restart with access.json
    # present leaves it None (TOFU branch is then unreachable anyway).
    if not ACCESS_FILE.exists():
        _TOFU_ENROLLMENT_CODE = secrets.token_hex(3)  # 6-char hex, 16M combinations
        print("", flush=True)
        print("  *** TOFU enrollment required ***", flush=True)
        print(f"  Enrollment code: {_TOFU_ENROLLMENT_CODE}", flush=True)
        print("  Send this code in your first DM to register as owner.", flush=True)
        print("  Anyone who sends this code first becomes owner — keep it private.", flush=True)
        print("", flush=True)

    offset = None
    allowed = load_allowed()
    pending_replies = {}  # task_id -> chat_id

    heartbeat_file = REPO / "state" / "telegram-bridge.heartbeat"
    last_heartbeat = 0
    while True:
        # Poll for new messages
        params = {"timeout": 10, "limit": 10}
        if offset:
            params["offset"] = offset
        try:
            result = api("getUpdates", **params)
        except Exception as e:
            print(f"[Telegram] Poll error: {e}", flush=True)
            time.sleep(5)
            continue
        if not result.get("ok"):
            # api() collapses an HTTPError (429 rate-limit / 5xx / auth) into
            # {"ok": False} with no detail. The loop used to fall straight
            # through and re-poll getUpdates with NO delay — which hammers
            # Telegram and, on a 429, *extends* its retry_after penalty (seen
            # in the bridge log as repeated "429 Too Many Requests: retry
            # after 5"). Back off the same 5s the exception path uses before
            # the next poll. (The `if result.get("ok")` below is now always
            # true; kept un-refactored to keep this fix a surgical diff.)
            time.sleep(5)
            continue
        # Heartbeat advances only on a response Telegram actually accepted.
        # A bumped heartbeat now means "the Telegram API round-trip is
        # working," not just "the asyncio loop is alive." Gated on
        # `result.get("ok")` because `api()` silently swallows HTTPError
        # (auth/rate-limit/500s) and returns `{"ok": False}`; without the
        # gate, those would still bump the heartbeat. Lets health-check
        # distinguish a zombie (process up, API dead) from a healthy
        # bridge. See: 2026-04-16 32h DNS-error zombie that had a fresh
        # heartbeat throughout because it was written before the try.
        if result.get("ok"):
            now = time.time()
            if now - last_heartbeat >= 60:
                try:
                    heartbeat_file.write_text(str(int(now)))
                    last_heartbeat = now
                except Exception:
                    pass
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                sender_id = str(msg["from"]["id"])
                username = msg["from"].get("username", sender_id)
                chat_id = msg["chat"]["id"]
                # The allowlist gates the SENDER, who may be writing from a group,
                # so record the chat audience for the progress placeholder's gate.
                chat_is_private = msg["chat"].get("type") == "private"
                text = msg.get("text", "")

                # Reload access list periodically
                allowed = load_allowed()
                if allowed is None:
                    # First-ever DM after install — access.json doesn't exist.
                    # Require the enrollment code before auto-onboarding as owner.
                    # This prevents an attacker who can DM the bot from claiming
                    # ownership before the legitimate owner does.
                    if _TOFU_ENROLLMENT_CODE and _TOFU_ENROLLMENT_CODE not in (text or ""):
                        api("sendMessage", chat_id=chat_id, text=(
                            "Enrollment code required.\n"
                            "Check the bridge startup log for your code and send it here."
                        ))
                        print(f"  TOFU: rejected enrollment from @{username} — code not presented", flush=True)
                        continue
                    allowed = tofu_onboard(sender_id, username)
                    # Do NOT clear _TOFU_ENROLLMENT_CODE after enrollment. If
                    # access.json is later deleted while the bridge keeps running
                    # (#899), load_allowed() returns None again; keeping the code
                    # valid for the process lifetime keeps the gate armed so the
                    # next DM must still present it, instead of falling through to
                    # an unguarded tofu_onboard(). Single secret, single owner.
                if sender_id not in allowed:
                    print(f"  Dropped message from non-allowed @{username}")
                    continue

                # Record owner activity for status-aware-pivot
                write_owner_activity("telegram", text, channel_id=chat_id)

                # Handle attachments (photos, documents, voice)
                attachment_note = ""
                # Structured refs (interaction-model 4D, step 1.5), accumulated
                # alongside the legacy [*attached:] body line — dual-write.
                attachment_refs: list = []  # pragma: no cover
                if "photo" in msg:
                    file_id = msg["photo"][-1]["file_id"]  # largest size
                    local_path = download_file(file_id, "photo")
                    if local_path:
                        attachment_note = f"\n[Photo attached: {local_path}]"
                        attachment_refs.append(local_task_protocol.AttachmentRef(  # pragma: no cover
                            locator=local_path, mime="image/jpeg",
                            filename=os.path.basename(local_path),
                            size=(msg["photo"][-1].get("file_size", 0) or 0)))
                        # If voice is connected, also push the photo as a
                        # vision frame so Gemini sees it in-stream (in
                        # addition to the file-attached task pipeline).
                        try:
                            _push_vision_image(local_path, source="telegram")
                        except Exception:
                            pass
                if "document" in msg:
                    file_id = msg["document"]["file_id"]
                    fname = msg["document"].get("file_name", "file")
                    local_path = download_file(file_id, fname)
                    if local_path:
                        attachment_note = f"\n[File attached: {local_path}]"
                        attachment_refs.append(local_task_protocol.AttachmentRef(  # pragma: no cover
                            locator=local_path,
                            mime=(msg["document"].get("mime_type", "") or ""),
                            filename=(fname or os.path.basename(local_path)),
                            size=(msg["document"].get("file_size", 0) or 0)))
                if "voice" in msg:
                    file_id = msg["voice"]["file_id"]
                    local_path = download_file(file_id, "voice.ogg")
                    if local_path:
                        attachment_refs.append(local_task_protocol.AttachmentRef(  # pragma: no cover
                            locator=local_path,
                            mime=(msg["voice"].get("mime_type", "") or "audio/ogg"),
                            filename=os.path.basename(local_path),
                            size=(msg["voice"].get("file_size", 0) or 0)))
                        transcript = _transcribe_via_skill(local_path)
                        if transcript:
                            attachment_note = f"\n[Voice transcript: {transcript}]"
                        else:
                            attachment_note = f"\n[Voice note attached: {local_path}]"

                if not text and not attachment_note:
                    continue

                forward_note = extract_forward_note(msg)

                safe_detail_log = filter_chat_secrets(
                    f"{redact_vault_commands(text)}{attachment_note}"
                ).text
                print(f"  @{username}{forward_note}: {safe_detail_log}")

                # Reply/parent context. Telegram embeds the full replied-to
                # message when this is a reply — capture it (+ message ids) so a
                # terse reply ("y", "no", a pronoun) is reconstructable. Unlike
                # Discord, the Bot API has NO message-history fetch, so this
                # embed-at-write quote is the ONLY reconstruct substrate for
                # Telegram (the agent falls back to the session transcript for
                # anything deeper than the immediate parent).
                reply_note = ""
                src_line = ""
                parent_line = ""
                _src_mid = msg.get("message_id")
                if _src_mid is not None:
                    src_line = f"source_message_id: {_src_mid}\n"
                _rep = msg.get("reply_to_message")
                if _rep:
                    _rep_user = (
                        _rep.get("from", {}).get("username")
                        or _rep.get("from", {}).get("first_name", "?")
                    )
                    _rep_text = (
                        _rep.get("text") or _rep.get("caption") or "[non-text message]"
                    ).replace("\n", " ")[:300]
                    reply_note = f"\n\n[Replying to @{_rep_user}: {_rep_text}]"
                    _rep_mid = _rep.get("message_id")
                    if _rep_mid is not None:
                        parent_line = f"parent_message_id: {_rep_mid}\n"

                # Write as task (same format as voice bridge)
                ts = int(time.time() * 1000)
                task_id = f"task-{ts}"
                task_file = TASKS_DIR / f"{task_id}.txt"
                priority = default_priority_for_source("telegram", "owner")

                # Intercept vault commands before disk write — Telegram treats
                # all senders as owner-tier (allowlist-gated bot token).
                if text:
                    vault_result = intercept_vault_commands(text)
                    text = vault_result.text
                    if vault_result.stored:
                        print(f"  [vault] stored keys: {vault_result.stored}", flush=True)
                    if vault_result.failed:
                        print(f"  [vault] store failed (still redacted): {vault_result.failed}", flush=True)

                filtered_text = filter_chat_secrets(text)
                filtered_attachment = filter_chat_secrets(attachment_note)
                filtered_reply = filter_chat_secrets(reply_note)
                text = filtered_text.text
                attachment_note = filtered_attachment.text
                reply_note = filtered_reply.text
                detected_secret_types = set(filtered_text.secret_types)
                detected_secret_types.update(filtered_attachment.secret_types)
                detected_secret_types.update(filtered_reply.secret_types)
                secret_notice = secret_handling_instruction(
                    "Telegram", detected_secret_types
                )

                # Inject skill instructions so the agent follows notify-before-work
                # and transcription protocol even after conversation compaction.
                # Only injected when the referenced skills are installed on this node.
                # Use claude_home_path() — honours $CLAUDE_CONFIG_DIR → $CLAUDE_HOME → ~/.claude
                # resolution order (inline os.environ.get misses the $CLAUDE_HOME fallback).
                _notify_py = claude_home_path("skills", "task-progress", "scripts", "notify.py")  # pragma: no cover
                _transcribe_py = claude_home_path("skills", "audio-transcribe", "scripts", "transcribe.py")  # pragma: no cover
                has_audio_attach = attachment_note and any(
                    attachment_note.lower().find(ext) != -1
                    for ext in (".m4a", ".mp3", ".ogg", ".opus", ".oga", ".wav", ".webm", ".aac")
                )
                lines = ["===SKILL INSTRUCTIONS (follow before any other action)==="]
                step = 1
                # CONTEXT-FIRST: reconstruct before interpreting. UNCONDITIONAL as of
                # 2026-07-13 (owner-approved): the prior form gated reconstruction on the
                # agent judging the message "not self-contained" — but that judgment ("I
                # already understand this") is the exact signal that fails, so the agent
                # kept walking past the read on questions it only *felt* confident about.
                # Removing the gate trades a little cheap re-reading for never skipping it;
                # only a pure greeting/ack is exempt. Unlike Discord, Telegram's Bot API has
                # NO message-history fetch — so the reconstruct substrate is the embedded
                # [Replying to …] quote (above) + the session transcript, NOT a channel
                # pull-back. Supersedes the self-contained-judgment form.
                lines.append(
                    f'{step}. CONTEXT-FIRST (unconditional): before interpreting this '
                    f'message, reconstruct context and answer from it, NOT from memory. '
                    f'Telegram has no message-history fetch, so the substrate is the '
                    f'embedded [Replying to …] quote above plus the session transcript — '
                    f'read them back until this message stands on its own. Do this every '
                    f'time; do NOT skip it because the message looks self-contained or you '
                    f'feel you already understand it — felt confidence is exactly the '
                    f'signal that fails. The only exception is a pure greeting or '
                    f'acknowledgement with no referent (e.g. "hi", "thanks").'
                )
                step += 1
                if _notify_py.exists():
                    notify_cmd = (
                        f"python3 {_notify_py}"
                        f" --source telegram --chat-id {chat_id}"
                    )
                    if has_audio_attach:
                        lines.append(f'{step}. NOTIFY FIRST: {notify_cmd} --message "Got your voice message, give me a moment."')
                    else:
                        lines.append(f'{step}. NOTIFY FIRST (if task takes >60s): {notify_cmd} --message "On it — back in a moment."')
                    step += 1
                if has_audio_attach and _transcribe_py.exists():
                    attached_path = attachment_note.split("[File attached: ")[-1].rstrip("]").split("\n")[0]
                    lines.append(f"{step}. TRANSCRIBE: python3 {_transcribe_py} '{attached_path}'")
                    step += 1
                lines.append(f"{step}. Process transcript and write result to results/{task_id}.txt")
                tg_skill_hints = "\n" + "\n".join(lines) + "\n"

                # interaction-model 4D, step 1.5: structured media headers
                # alongside the legacy [*attached:] body line (dual-write). Real
                # headers after `task:`, so confine_user_content defangs a forged
                # body copy while these authentic ones pass through.
                media_headers = local_task_protocol.media_attachment_headers(  # pragma: no cover
                    attachment_refs, bool(text and text.strip()))
                _task_content = (
                    f"id: {task_id}\n"
                    f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                    f"source: telegram\n"
                    f"interaction_type: message\n"
                    f"{media_headers}"
                    f"chat_id: {chat_id}\n"
                    f"{src_line}"
                    f"{parent_line}"
                    f"priority: {priority}\n"
                    f"task: {confine_user_content(f'[Telegram @{username}{forward_note}] {text}{attachment_note}{reply_note}')}\n"
                    f"{tg_skill_hints}"
                    f"{secret_notice}"
                )
                # HMAC envelope (#3014 writer census): stamp at this writer's edge,
                # fail-open so a stamping error costs the stamp and never the task.
                try:
                    from task_envelope import stamp_text  # sibling (src/ on sys.path)
                    _task_content = stamp_text(_task_content, REPO)
                except Exception:
                    pass
                task_file.write_text(_task_content)
                pending_replies[task_id] = chat_id
                pending_task_tiers[task_id] = "owner"  # telegram is owner-only (allowlist-gated); enables progress streaming
                pending_task_private[task_id] = chat_is_private  # audience, not sender: gates the step text
                # Observability: one inbound accepted-message event. Source the
                # tier from the bridge's own assignment above (single source of
                # truth) rather than re-asserting a literal here.
                _emit_channel(
                    "telegram", "in",
                    user_id=str(chat_id),
                    channel_id=str(chat_id),
                    access_tier=pending_task_tiers[task_id],
                    data={"task_id": task_id, "has_attachment": bool(attachment_note)},
                )
                # Anonymous, opt-out product telemetry: one bucketed event per
                # accepted task, tagged only with the inbound surface. No-op when
                # opted out / no key; never task content or ids. See
                # src/telemetry.py + TELEMETRY.md.
                try:  # pragma: no cover — fire-and-forget; logic tested in tests/telemetry.test.py
                    from telemetry import task_processed  # sibling module (src/ on sys.path)

                    task_processed("telegram")
                except Exception:  # pragma: no cover — telemetry must never break the bridge
                    pass

                # Send typing indicator
                api("sendChatAction", chat_id=chat_id, action="typing")

        # Check for proactive messages to send to owner.
        # Presenter-mode: retain files (don't unlink, don't send) so they
        # flush after the talk window ends. See presenter-mode.sh contract.
        # Channel routing: skip the proactive scan entirely if telegram
        # is not the last-active channel. Pre-fix the discord-bridge
        # and telegram-bridge raced for the SAME proactive-*.txt files
        # and whichever ran first delivered, producing cross-channel
        # surprises. See proactive_routing.py for the decision rule.
        from proactive_routing import (proactive_body_guard,
                                       should_claim_proactive_file)
        try:
            if not presenter_mode_active(REPO):
                # discord-bridge.poll_dm_fallback handles briefing-/insight-/
                # friction-*.txt via FALLBACK_PREFIXES; telegram-bridge only
                # matched `proactive-`, so morning-briefing output (which
                # writes `results/briefing-{date}.txt` per the skill
                # contract) was silently archived without reaching Telegram.
                # Treat the same prefixes as proactive-equivalent so
                # cron-originated results land in the owner's DM regardless
                # of which bridge is the active channel.
                PROACTIVE_PREFIXES = ("proactive-", "briefing-", "insight-", "friction-")
                def _tg_claims(name: str) -> bool:
                    # Destination outranks activity routing, uniformly;
                    # discord's DM fallback stays the after-grace catch-all.
                    return should_claim_proactive_file(
                        name, OWNER_ACTIVITY_FILE, "telegram",
                        body_reader=lambda _n=name: read_ready_result(RESULTS_DIR / _n))

                for f in RESULTS_DIR.iterdir():
                    if any(f.name.startswith(p) for p in PROACTIVE_PREFIXES) \
                            and f.suffix == ".txt" and _tg_claims(f.name):
                        # Peek before claiming: a body addressed to another bridge is
                        # delivered there — unless the FILENAME destines it here.
                        try:
                            peek = f.read_text(errors="ignore").lstrip()
                        except OSError:
                            continue
                        if not proactive_body_guard(f.name, peek, "telegram"):
                            continue
                        # Resolve the recipient BEFORE claiming: the claim renames the
                        # file out of the `*.txt` glob every peer bridge polls.
                        # `_resolve_proactive_owner_id` orders allowFrom explicitly; a
                        # bare `next(iter(set))` picked a hash-slot, not the first user.
                        env_override = (config_get_env_first("SUTANDO_DM_OWNER_ID", "") or "").strip()
                        try:
                            access_data = json.loads(ACCESS_FILE.read_text())
                        except Exception:
                            access_data = {}
                        owner_id = _resolve_proactive_owner_id(env_override, access_data)
                        if owner_id is None:
                            continue
                        # The shared helper owns "no recipient -> do not claim"; this
                        # adapter contributes only the owner it resolved.
                        claim = claim_for_delivery(f, owner_id)
                        if claim is None:
                            continue
                        f = claim
                        text = read_ready_result(f)
                        if text is None:
                            release_claim(f)
                            continue
                        # The claim hard-links then unlinks, so a producer still
                        # holding the original fd keeps writing THIS inode.
                        if not proactive_body_guard(
                                f.with_suffix(".txt").name, text, "telegram"):
                            release_claim(f)
                            continue
                        try:
                            _s = send_reply(int(owner_id), text)
                            if _s["text_chunks"] or _s["files_sent"]:
                                _emit_channel(
                                    "telegram", "out",
                                    user_id=str(owner_id),
                                    channel_id=str(owner_id),
                                    access_tier="owner",  # proactive → owner
                                    outcome="ok" if _s["ok"] else "error",
                                    data={"text_chunks": _s["text_chunks"], "file_count": _s["files_sent"]},
                                )
                            if _s.get("ok"):
                                print(f"  [proactive] sent to {owner_id}: {text[:80]}")
                                f.unlink(missing_ok=True)
                            else:
                                # send_reply reports refusal by returning ok=False
                                # without raising; the except below never sees it.
                                print(f"  [proactive] send refused, releasing {f.name}")
                                release_claim(f)
                        except Exception as e:
                            # Release, never delete: pollers scan `.txt`, so a deleted
                            # claim is a message no bridge can ever retry.
                            print(f"  [proactive] failed, releasing {f.name}: {e}")
                            release_claim(f)
        except Exception as e:
            print(f"  [proactive] poll error: {e}")

        # Opt-in: stream live progress for long owner tasks (no-op unless enabled).
        try:
            poll_progress(pending_replies)
        except Exception as e:
            print(f"[Telegram] poll_progress error: {e}", flush=True)

        # Check for results to send back (includes any orphaned-by-restart
        # routing recovered from the task files themselves — see
        # _gather_pending_task_ids for why this must run every tick).
        for task_id in _gather_pending_task_ids(pending_replies, RESULTS_DIR, TASKS_DIR):
            result_file = RESULTS_DIR / f"{task_id}.txt"
            if result_file.exists():
                reply_text = read_ready_result(result_file)
                if reply_text is None:
                    continue
                chat_id = pending_replies.pop(task_id)
                # Parse markers via the unified module (#873). Telegram
                # honors [no-send] / [REPLIED] / [deduped: <id>] as skip,
                # sends attached files, and silently drops [channel:] redirects
                # (no concept in Telegram). Pass parsed.body so NO marker ever
                # leaks as literal text in the user's DM (#1381).
                parsed = parse_markers(reply_text)
                if any(a.kind == "skip" for a in parsed.actions):
                    _sk = next(a for a in parsed.actions if a.kind == "skip")
                    if _sk.value == "deduped":
                        _rq, _disp = _dedup_recover(task_id, _sk.extra, chat_id)
                        if _rq:
                            pending_replies[_rq] = chat_id
                        if _disp == "retain":
                            print(f"  [dedup] report not delivered for {task_id} "
                                  f"— keeping for retry", flush=True)
                            continue
                    print(f"  Skipped (marker): {task_id}", flush=True)
                    _clear_progress(task_id)  # remove any progress placeholder + tier tracking
                    archive_file(result_file, "results", task_id)
                    task_file = find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt"
                    archive_file(task_file, "tasks", task_id)
                    continue
                # Bound before the try: a raise below must not leave the archive
                # gate reading an unassigned name.
                delivered_ok = False
                try:
                    # Use parsed.body — all markers stripped — so [channel:] etc. never leak.
                    # File attachments are in parsed.actions; send_reply() won't re-find them,
                    # so send them here and fold the result into ONE obs event below.
                    _tier = pending_task_tiers.get(task_id, "unknown")
                    _s = send_reply(chat_id, parsed.body, task_id=task_id)
                    delivered_ok = _s["ok"]
                    sent_files = _s["files_sent"]
                    for action in parsed.actions:
                        if action.kind == "attach":
                            fpath = action.value.strip()
                            if _is_path_sendable(fpath):
                                resp = send_file(chat_id, fpath)
                                if isinstance(resp, dict) and resp.get("ok"):
                                    sent_files += 1
                                    print(f"  Sent file: {fpath}", flush=True)
                                else:
                                    delivered_ok = False
                                    print(f"  Send file failed: {fpath}", flush=True)
                            elif os.path.isfile(fpath):
                                api("sendMessage", chat_id=chat_id, text=f"(file access denied: {fpath})")
                                print(f"  BLOCKED file: {fpath}")
                            else:
                                print(f"  file marker, file not found — likely a prose quotation: {fpath}", flush=True)
                    # Observability: one delivered-reply event covering text +
                    # externally-sent attachments. outcome reflects real success;
                    # file_count is files actually delivered (api/send_file swallow
                    # errors, so we must not assume "ok").
                    if _s["text_chunks"] or sent_files or not delivered_ok:
                        _emit_channel(
                            "telegram", "out",
                            user_id=str(chat_id),
                            channel_id=str(chat_id),
                            access_tier=_tier,
                            outcome="ok" if delivered_ok else "error",
                            data={"task_id": task_id, "text_chunks": _s["text_chunks"], "file_count": sent_files},
                        )
                    if delivered_ok:
                        print(f"  Replied to {chat_id}: {parsed.body[:80]}...", flush=True)
                except Exception as e:
                    print(f"[Telegram] Reply error: {e}", flush=True)
                if not delivered_ok:
                    # Restore the route popped above, or a retry has no chat_id.
                    pending_replies[task_id] = chat_id
                    print(f"[Telegram] reply refused, keeping {task_id} for retry", flush=True)
                    continue
                _clear_progress(task_id)  # remove any progress placeholder + tier tracking
                # Archive (not delete) so we can mine patterns later.
                archive_file(result_file, "results", task_id)
                task_file = find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt"
                archive_file(task_file, "tasks", task_id)

        time.sleep(1)

if __name__ == "__main__":
    main()
