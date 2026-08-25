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
import shlex
import shutil
import sys
import threading
import time
import urllib.request
from collections import deque
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
from optional_script import run_optional_script as _run_optional_script_shared  # noqa: E402
from presenter_mode import presenter_mode_active  # noqa: E402
from proactive_recovery import (claim_for_delivery, recover_orphan_sending_files,  # noqa: E402
                                release_claim)
from proactive_routing import body_claimable_by, fallback_claims_name  # noqa: E402


def _slack_claims_name(name: str) -> bool:
    """Filename-level claim decision — the policy lives in proactive_routing;
    this adapter only binds its channel."""
    return fallback_claims_name(name, "slack")
from owner_activity import write_owner_activity as _write_owner_activity_shared  # noqa: E402
import slack_access  # noqa: E402

# Observability: emit channel.slack.<in|out> into the local obs spine
# (src/observability). Guarded so a missing module never crashes the bridge.
try:
    from observability.channel import emit_channel as _emit_channel  # noqa: E402
except Exception:  # pragma: no cover — best-effort telemetry
    def _emit_channel(*_a, **_k):  # type: ignore
        return None
from result_markers import parse_markers  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402
from dedup_recovery import plan_dedup_recovery, report_disposition  # noqa: E402
from message_chunking import chunk_message  # noqa: E402  (Result Router S3 — shared fence-aware chunker)
import local_task_protocol  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402
from util_paths import channel_access_path, claude_home_path, write_private_text  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from task_archive import find_task_file  # noqa: E402
from task_archive import archive_file as _shared_archive_file  # noqa: E402
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
_THREAD_CONTEXT_MAX_MESSAGES = 20
_THREAD_CONTEXT_MESSAGE_MAX_CHARS = 500
_THREAD_CONTEXT_PAGE_SIZE = 100
_THREAD_CONTEXT_MAX_PAGES = 10
# Durable on-disk backup of the Slack access allowlist. The in-memory
# _access_cache restores access.json after an intermittent wipe (#899) ONLY
# while the process lives; a wipe + process restart (observed 2026-07-17 —
# the bridge booted into TOFU and would have enrolled the next DM'er as owner)
# loses the cache. This backup lives under state/auth/ (per CLAUDE.md, the
# cleanup-exempt per-host install-state dir) so a restart can restore the
# allowlist from disk instead of falling through to a security-exposing TOFU.
ACCESS_BACKUP_FILE = STATE_DIR / "auth" / "slack-access-backup.json"
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
    # Last resort: the Keychain vault, per token independently — a host may have
    # one in the .env and the other only vaulted.
    from channel_token import token_from_vault  # noqa: E402
    BOT_TOKEN = BOT_TOKEN or token_from_vault("SLACK_BOT_TOKEN")
    APP_TOKEN = APP_TOKEN or token_from_vault("SLACK_APP_TOKEN")

if not BOT_TOKEN or not APP_TOKEN:
    print("SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN not set", file=sys.stderr)
    sys.exit(1)


# Outbound attachment allowlisting is canonical policy — src/send_allowlist.py
# is the single source of truth (a hand-written copy here drifted from it and
# silently dropped files other bridges would send). Slack extends it with its
# OWN inbound dir so an uploaded file can be echoed back; that root stays
# Slack-local rather than becoming global.
from policy.egress.attachment import is_path_sendable as _is_path_sendable_canonical  # noqa: E402


def _is_path_sendable(fpath: str) -> bool:
    """Canonical allowlist + Slack's inbound dir. See src/send_allowlist.py."""
    return _is_path_sendable_canonical(fpath, extra_roots=(str(INBOX_DIR),))


def write_owner_activity(channel: str, summary: str, channel_id=None) -> None:
    """Record owner activity using the shared provider-neutral schema."""
    _write_owner_activity_shared(
        OWNER_ACTIVITY_FILE,
        channel,
        summary,
        channel_id,
        on_error=lambda exc: print(
            f"  [owner-activity] write failed: {exc}", flush=True
        ),
    )


def archive_file(src: Path, kind: str, task_id: str) -> bool:
    """Adapter: inject this bridge's archive roots + logger into the shared
    never-delete policy. It used to unlink the source when the move failed,
    which destroyed the only copy of the task."""
    return _shared_archive_file(
        src, kind, task_id,
        tasks_dir=ARCHIVE_TASKS_DIR, results_dir=ARCHIVE_RESULTS_DIR,
        log=lambda m: print(f"[Slack]{m}", flush=True))


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
    _backup_access_to_disk(data)


def _is_valid_access_doc(data) -> bool:
    """A structurally valid access-control document worth backing up / restoring.

    The core schema is an ``allowFrom`` list. Three states qualify and MUST be
    protected, none of which ``tofuOwner`` alone covers (CR #2163, qingyun-wu):
      - a TOFU-enrolled allowlist (has ``tofuOwner``);
      - a legacy populated allowlist enrolled before ``tofuOwner`` existed;
      - the intentional locked-down state ``allowFrom: []``.
    Only a transient/partial wipe — a non-dict, a parse failure, or a
    missing/non-list ``allowFrom`` — is rejected, so it can't overwrite a good
    backup. (A slack wipe deletes access.json rather than rewriting it to empty,
    so an empty allowlist reaches this path only when it was written on purpose.)
    """
    return isinstance(data, dict) and isinstance(data.get("allowFrom"), list)


def _backup_access_to_disk(data: dict) -> None:
    """Persist a copy of a VALID access-control document to the durable backup.
    Backs up any structurally valid state (see ``_is_valid_access_doc``) —
    including a legacy populated allowlist or an intentional empty lockdown —
    but never a transient/partial wipe, so a wipe can't overwrite the good
    backup."""
    if not _is_valid_access_doc(data):
        return
    try:
        ACCESS_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private_text(ACCESS_BACKUP_FILE, json.dumps(data, indent=2) + "\n")
    except OSError:
        pass  # best-effort; backup must never break the write path


def _restore_access_from_disk() -> bool:
    """Restore access.json from the durable on-disk backup. Survives process
    death (unlike the in-memory cache), closing the wipe+restart -> TOFU
    exposure. Returns True if restored."""
    try:
        backup = json.loads(ACCESS_BACKUP_FILE.read_text())
    except Exception:
        return False
    if not _is_valid_access_doc(backup):
        return False
    try:
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private_text(ACCESS_FILE, json.dumps(backup, indent=2) + "\n")
        _update_access_cache(backup)
        print(
            "  [access] restored access.json from durable on-disk backup "
            "(wipe survived a restart — #899 defense-in-depth)",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"  [access] disk-backup restore failed: {e}", flush=True)
        return False


def _restore_access_from_cache() -> bool:
    """Write _access_cache back to ACCESS_FILE. Returns True if restored."""
    with _access_cache_lock:
        cached = _access_cache
    if not cached or not cached.get("tofuOwner"):
        return False
    try:
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private_text(ACCESS_FILE, json.dumps(cached, indent=2) + "\n")
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
    access = slack_access.read_access(ACCESS_FILE)
    # Only a record the shared reader accepted is worth caching; a malformed one
    # would overwrite a good backup with a document nobody can authenticate from.
    if access.record is not None:
        _update_access_cache(access.record)
    return access.allowed


def load_tier_map() -> dict:
    """Return the per-user-id → tier map from access.json `tierMap`, or
    empty dict if missing. Recognized tiers: "owner", "team", "other".
    Unmapped users are resolved by the caller as "other" (fail-safe): owner
    comes STRICTLY from membership in a successfully-persisted map.
    `_ensure_tier_map_seeded()` grandfathers existing members into a non-empty
    map, so a legacy no-tierMap config still resolves its members to owner via
    that seeded map — while an empty/unconfirmed map (seed could not persist)
    fails closed to "other" rather than escalating to owner (#2161)."""
    with _access_cache_lock:
        cached = _access_cache
        cached_mtime = _access_cache_mtime
    if cached is not None:
        try:
            if ACCESS_FILE.stat().st_mtime == cached_mtime:
                tier_map = cached.get("tierMap")
                return tier_map if isinstance(tier_map, dict) else {}
        except OSError:
            pass  # file deleted — fall through to re-read (will return {})
    try:
        data = json.loads(ACCESS_FILE.read_text())
        _update_access_cache(data)
        tier_map = data.get("tierMap")
        return tier_map if isinstance(tier_map, dict) else {}
    except Exception:
        return {}


def _ensure_tier_map_seeded() -> bool:
    """One-time migration: if access.json has a populated allowFrom but no
    tierMap, seed tierMap from allowFrom (all existing members -> owner) and
    persist. Idempotent: does nothing once a tierMap exists. This flips the
    default for FUTURE allowlist additions to read-only (they'll be missing
    from tierMap -> resolved as "other") without demoting anyone already
    trusted. Owner request 2026-07-17: allowlist default should be read-only.

    Returns True when a tierMap is reliably in place afterward (already
    present, just persisted, or nothing to seed); False when a seed was
    needed but could NOT be persisted/read. On False the caller MUST fail
    closed — never grant owner off an empty/unconfirmed map (#2161 CR:
    a transient read/write error must not silently escalate every
    allowlisted user to owner).
    """
    try:
        data = json.loads(ACCESS_FILE.read_text())
    except Exception as e:
        print(f"  [tier-map] WARNING: access.json unreadable ({e}); allowlisted users resolve read-only (other) until the tierMap can be read", flush=True)
        return False
    allow = data.get("allowFrom") or []
    # Test key PRESENCE, not truthiness. An explicitly-empty tierMap ({}) is a
    # deliberate "nobody is owner via tierMap" state — treating it as falsy
    # here would re-seed every allowFrom member as owner, escalating read-only
    # users (#2161 CR: {"allowFrom":["U"],"tierMap":{}} must NOT become
    # {"U":"owner"}). Only a genuinely ABSENT key (never-seeded legacy file)
    # triggers first-run grandfathering below. A present-but-empty map returns
    # here, so the allowlisted user is missing from the map and resolves other.
    if "tierMap" in data:
        return True
    if not allow:
        return True  # nothing to grandfather — an empty map is legitimate here
    data["tierMap"] = {uid: "owner" for uid in allow}
    # Atomic write: a bare ACCESS_FILE.write_text() truncates the live
    # access-control file BEFORE writing, so a disk-full / interrupt / partial
    # write can destroy allowFrom — and with fail-closed tier resolution that
    # locks legitimate owners out against a corrupt file, at bridge startup.
    # Write a sibling temp BORN 0600 (write_private_text), then os.replace(). The pid+uuid
    # suffix avoids colliding with a concurrent .tmp; on any failure the original
    # access.json bytes are left intact and the orphan temp is removed.
    tmp = ACCESS_FILE.with_suffix(ACCESS_FILE.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        write_private_text(tmp, json.dumps(data, indent=2) + "\n")
        os.replace(tmp, ACCESS_FILE)
        _update_access_cache(data)
        print(f"  [tier-map] grandfathered {len(allow)} existing allowFrom member(s) as owner; new additions now default to read-only", flush=True)
        return True
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"  [tier-map] WARNING: failed to persist grandfather tierMap ({e}); allowlisted users resolve read-only (other) until seeded", flush=True)
        return False


def resolve_access_tier(user_id: str, tier_map: dict, seeded_ok: bool) -> str:
    """Resolve a sender's access_tier from a loaded tierMap. Owner comes
    STRICTLY from membership in a successfully-persisted map:
      1. uid present in tierMap        → its recorded tier (owner/team/other)
      2. tierMap present, uid missing  → "other" (a new allowlist addition;
         prevents silent privilege escalation when the operator forgets a
         tierMap line)
      3. tierMap empty/unconfirmed     → "other" (fail CLOSED). A legit
         pre-tierMap config is grandfathered into a NON-empty map by
         _ensure_tier_map_seeded, so an empty map here means the seed could
         not persist/read — never grant owner off that (#2161 CR: a transient
         error must not escalate every allowlisted user to owner).
      4. Unknown tier value in config  → degrade safely to "other" rather
         than treating as owner.
    See #893 for the split-default rationale; #2161 for the fail-closed fix.
    Exercised directly by tests/slack-bridge-tier-map.test.py (#2512).
    """
    if user_id in tier_map:
        access_tier = tier_map[user_id]
    else:
        access_tier = "other"
        if tier_map:
            print(
                f"  [tier-map] WARNING: User {user_id} in allowFrom but missing from tierMap; defaulting to 'other'",
                flush=True,
            )
        elif not seeded_ok:
            print(
                f"  [tier-map] WARNING: grandfather seed unavailable; {user_id} resolved read-only (other), not owner",
                flush=True,
            )
    if access_tier not in ("owner", "team", "other"):
        access_tier = "other"
    return access_tier


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
    # In-memory cache is empty too (e.g. this is a fresh process after a
    # wipe + restart). Try the durable on-disk backup before enrolling —
    # otherwise a wiped allowlist would TOFU-enroll the next DM'er as owner.
    if _restore_access_from_disk():
        return load_allowed() or set()
    # Genuine first-time TOFU.
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "allowFrom": [user_id],
        "tierMap": {user_id: "owner"},  # TOFU enrollee is the owner; explicit
        "tofuOwner": user_id,
        "tofuOnboardedAt": int(time.time()),
        "tofuOnboardedUsername": username or None,
    }
    write_private_text(ACCESS_FILE, json.dumps(payload, indent=2) + "\n")
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


def _dedup_recover(task_id: str, holder_id, target) -> str:
    """Route the shared dedup-recovery plan; Slack owns only the send.

    Returns the shared disposition: "archive" once the exchange is terminal,
    "retain" when the asker was never told and a later pass must retry.
    """
    action, delivered = "defer", None
    try:
        action, payload = plan_dedup_recovery(
            RESULTS_DIR, TASKS_DIR, task_id, holder_id,
            (target or {}).get("channel", ""), f"task-{int(time.time() * 1000)}")
        if action == "requeue":
            _set_pending_reply(payload, dict(target or {}))
            print(f"  [dedup] re-queued {task_id} as {payload}", flush=True)
        elif action == "report" and target:
            # The boolean is the whole point: an unsent report tells the asker
            # nothing, so retiring on the attempt loses the question.
            delivered = bool(_send_reply(
                target["channel"], target.get("thread_ts"), payload,
                task_id=task_id, access_tier=target.get("access_tier", "unknown")))
            print(f"  [dedup] unresolved for {task_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the disposition, not the raise, decides
        print(f"  [dedup] recovery failed for {task_id}: {exc}", flush=True)
    return report_disposition(action, delivered)


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
        # HMAC envelope (#3014 writer census): stamp at this writer's edge,
        # fail-open so a stamping error costs the stamp and never the task.
        try:
            from task_envelope import stamp_text  # sibling module (src/ on sys.path)
            content = stamp_text(content, REPO)
        except Exception:
            pass
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
            # Read only the small prefix needed for the HTML/login-page guard,
            # then stream the remainder. This preserves the bounded-memory fix
            # while retaining main's files:read failure detection.
            head = resp.read(64)
            # When the bot token lacks the files:read scope (or the file is
            # otherwise unauthorized), Slack does NOT error — it 200s with an
            # HTML sign-in page. Persisting that page as e.g. a ".m4a" silently
            # corrupts the attachment: downstream transcription/parsing then
            # chokes on a login page. Detect it and fail cleanly instead so the
            # caller surfaces "no attachment" rather than feeding garbage on.
            ctype = (getattr(resp, "headers", {}).get("Content-Type") or "").lower()
            looks_html = ctype.startswith("text/html") or head.lstrip()[:14].lower() == b"<!doctype html"
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
                f.write(head)
                shutil.copyfileobj(resp, f)
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


def _slack_actor_label(message: dict) -> str:
    """Return a compact, identity-resolved label for one Slack message."""
    user_id = str(message.get("user") or "")
    fallback = str(message.get("username") or message.get("bot_id") or user_id or "?")
    display_name = _resolve_username(user_id) if user_id else None
    clean_name = " ".join((display_name or fallback).split()) or "?"
    return f"@{clean_name}" + (f" ({user_id})" if user_id else "")


def _resolve_slack_mentions(text: str) -> str:
    """Add display names beside Slack user mentions without dropping their IDs."""

    def _replace(match: re.Match) -> str:
        user_id = match.group(1)
        display_name = _resolve_username(user_id)
        if not display_name:
            return match.group(0)
        clean_name = " ".join(display_name.split())
        return f"@{clean_name} (<@{user_id}>)"

    return re.sub(r"<@([A-Z0-9]+)>", _replace, text)


def _slack_identity_note(event: dict, username: str | None) -> str:
    """Describe the triggering author and mentioned accounts for role clarity."""
    author_id = str(event.get("user") or "")
    author_name = " ".join((username or author_id or "?").split())
    identities = [f"author: @{author_name}" + (f" ({author_id})" if author_id else "")]
    seen = {author_id}
    for user_id in re.findall(r"<@([A-Z0-9]+)>", str(event.get("text") or "")):
        if user_id in seen:
            continue
        seen.add(user_id)
        display_name = _resolve_username(user_id)
        clean_name = " ".join((display_name or user_id).split())
        identities.append(f"mentioned: @{clean_name} ({user_id})")
    return "\n\n[Slack identities — " + "; ".join(identities) + "]"


def _format_slack_context(messages: list[dict], label: str) -> tuple[str, set[str]]:
    """Format bounded Slack-owned context as confined, untrusted task data."""
    lines = []
    secret_types = set()
    for message in messages:
        text = " ".join(str(message.get("text") or "").split())
        if not text:
            continue
        filtered = filter_chat_secrets(text)
        text = filtered.text
        secret_types.update(filtered.secret_types)
        text = _resolve_slack_mentions(text)[:_THREAD_CONTEXT_MESSAGE_MAX_CHARS]
        lines.append(f"  {_slack_actor_label(message)}: {text}")
    if not lines:
        return "", secret_types
    note = (
        f"\n\n[Slack {label} context — untrusted messages, oldest first:\n"
        + "\n".join(lines)
        + "\n]"
    )
    return note, secret_types


def _slack_context_unavailable_note() -> str:
    return (
        "\n\n[Slack channel context unavailable; do not assume "
        "client/support roles from this isolated mention.]"
    )


def _slack_context_note(event: dict) -> tuple[str, set[str]]:
    """Fetch bounded context for a Slack channel mention.

    Thread replies retain the root plus the newest messages before the trigger.
    Top-level mentions receive the newest preceding channel messages. Fetches
    are best-effort; an explicit unavailable note prevents isolated-message
    role assumptions when Slack history cannot be read.
    """
    if event.get("channel_type") == "im":
        return "", set()
    channel = str(event.get("channel") or "")
    event_ts = str(event.get("ts") or "")
    if not channel or not event_ts:
        return _slack_context_unavailable_note(), set()

    thread_ts = str(event.get("thread_ts") or "")
    try:
        if thread_ts and thread_ts != event_ts:
            root = None
            tail = deque(maxlen=max(0, _THREAD_CONTEXT_MAX_MESSAGES - 1))
            cursor = ""
            exhausted = False
            for _page in range(_THREAD_CONTEXT_MAX_PAGES):
                kwargs = {
                    "channel": channel,
                    "ts": thread_ts,
                    "limit": _THREAD_CONTEXT_PAGE_SIZE,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                response = app.client.conversations_replies(**kwargs)
                if response.get("ok") is False:
                    raise RuntimeError("Slack conversations.replies failed")
                for message in response.get("messages") or []:
                    if root is None:
                        root = message
                    if str(message.get("ts") or "") in (thread_ts, event_ts):
                        continue
                    tail.append(message)
                cursor = str(
                    (response.get("response_metadata") or {}).get("next_cursor") or ""
                )
                if not cursor:
                    exhausted = True
                    break
            messages = ([root] if root else []) + list(tail)
            note, secret_types = _format_slack_context(messages, "thread")
            if not note:
                return _slack_context_unavailable_note(), secret_types
            if not exhausted:
                note += "\n[Slack thread context truncated before the triggering reply.]"
            return note, secret_types

        response = app.client.conversations_history(
            channel=channel,
            latest=event_ts,
            inclusive=False,
            limit=_THREAD_CONTEXT_MAX_MESSAGES,
        )
        if response.get("ok") is False:
            raise RuntimeError("Slack conversations.history failed")
        # conversations.history is newest-first; task context reads oldest-first.
        messages = list(reversed(response.get("messages") or []))
        note, secret_types = _format_slack_context(messages, "channel")
        return (note or _slack_context_unavailable_note()), secret_types
    except Exception:
        return _slack_context_unavailable_note(), set()


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

    # Redact first: a pasted token must never reach state JSON even in a preview.
    initial_secret_filter = filter_chat_secrets(text)
    detected_secret_types = set(initial_secret_filter.secret_types)
    safe_attachment = filter_chat_secrets(attachment_note)
    detected_secret_types.update(safe_attachment.secret_types)

    channel = event.get("channel", "")
    # Reply in-thread for channel @mentions, top-level for DMs. parens for
    # readability; Python's `or` + ternary precedence is correct here but
    # the explicit grouping makes the intent obvious to humans.
    if event.get("channel_type") != "im":
        thread_ts = event.get("thread_ts") or event.get("ts")
    else:
        thread_ts = None
    # Only an existing Slack thread is a valid target for the immediate
    # progress update. A top-level mention still uses its message timestamp for
    # final-result routing above, but must not make the progress update create a
    # separate thread before the task result arrives.
    event_thread_ts = event.get("thread_ts")
    reply_thread_ts = (
        event_thread_ts
        if thread_ts and event_thread_ts and event_thread_ts != event.get("ts")
        else None
    )

    # Resolve access_tier from `tierMap`. The fail-closed rules live on
    # resolve_access_tier's docstring, and tests/slack-bridge-tier-map.test.py
    # exercises that same symbol (#2512 — coverage claim now matches reality).
    seeded_ok = _ensure_tier_map_seeded()
    tier_map = load_tier_map()
    access_tier = resolve_access_tier(user_id, tier_map, seeded_ok)

    # Owner-only, as discord-bridge does: the proactive loop reads this file as
    # owner PRESENCE, so a team/other sender stamping it fakes "owner is here".
    if access_tier == "owner":
        write_owner_activity(
            "slack",
            initial_secret_filter.text or safe_attachment.text,
            channel_id=event.get("channel"),
        )

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
    # Prepend an in-band system instruction for non-owner tiers so the
    # core agent cannot accidentally process a downgraded task with full
    # capabilities. Mirrors the Discord bridge's tier-specific instruction
    # block (see discord-bridge.py around `===SUTANDO SYSTEM INSTRUCTIONS===`).
    # Kept short here because Slack's downgrade surface today is just
    # "delegate to sandboxed read-only agent". Thread context is prefetched
    # below because that sandbox intentionally has no Slack token or network.
    # Confine the user-derived portion BEFORE the bridge appends its own
    # `===SUTANDO SYSTEM INSTRUCTIONS===` block below — so a forged field/fence
    # in the message can't escalate tier or inject instructions, while the
    # bridge's legitimate fence (added next) stays intact. See task_body_guard.

    # Channel mentions carry bounded Slack-owned context plus an explicit
    # identity map. The entire substrate remains confined as untrusted task
    # data before any trusted tier instruction is appended.
    _identity_note = (
        "" if event.get("channel_type") == "im"
        else _slack_identity_note(event, username)
    )
    _context_note, _context_secret_types = _slack_context_note(event)
    detected_secret_types.update(_context_secret_types)
    secret_notice = secret_handling_instruction("Slack", detected_secret_types)
    user_task_text = confine_user_content(
        f"[{prefix} @{username or user_id}] {text}{attachment_note}"
        f"{_identity_note}{_context_note}"
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
    _claude_config_dir = claude_home_path()
    skill_hints = ""
    # CONTEXT-FIRST is a correctness step, not a skill hint: it must not be
    # gated on optional skills. The steps that need them stay conditional inside.
    if access_tier == "owner":
        hints_lines = ["===SKILL INSTRUCTIONS (follow before any other action)==="]
        step = 1
        hints_lines.append(
            f'{step}. CONTEXT-FIRST: if this message is not self-contained '
            f'(terse — "y", "no", a pronoun — a reply, or refers to something not '
            f'stated here), reconstruct context BEFORE interpreting. Slack has no '
            f'channel-history fetch in this bridge, so use the embedded thread/reply '
            f'context above (if present) plus the session transcript, and answer from '
            f'that, not from memory.'
        )
        step += 1
        if _notify_py.exists():
            notify_thread_arg = (
                f" --thread-ts {shlex.quote(str(reply_thread_ts))}"
                if reply_thread_ts else ""
            )
            notify_cmd = (
                f"env CLAUDE_CONFIG_DIR={shlex.quote(str(_claude_config_dir))} "
                f"python3 {shlex.quote(str(_notify_py))}"
                f" --source slack --channel-id {channel}"
                f"{notify_thread_arg}"
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
    reply_thread_header = (
        f"reply_thread_ts: {reply_thread_ts}\n" if reply_thread_ts else ""
    )
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
        f"{reply_thread_header}"
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

# A split turn is near-simultaneous; a same-user message from far back in the
# page is a stale instruction, so bound recovery to a short window.
_EMPTY_MENTION_RECOVERY_MAX_AGE_S = 600  # 10 minutes


def _resolve_mention_text(event: dict, stripped_text: str) -> tuple[str, bool]:
    """Recover an empty mention from the sender's prior turn: same thread and sender
    only, so no one else's text is attributed to them; else a clarification task."""
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
                # A bot reply means the prior turn was already answered, so stop rather than
                # skipping past it and re-running that instruction.
                if message.get("bot_id"):
                    break
                # Do not jump across another human participant and attribute an
                # older instruction to the current sender.
                if message.get("user") != user_id:
                    break
                # Newest-first: once one message is past the window the rest are older, so
                # stop rather than continue.
                try:
                    if float(current_ts) - float(message.get("ts", "0")) > _EMPTY_MENTION_RECOVERY_MAX_AGE_S:
                        break
                except (TypeError, ValueError):
                    # Unknown age must fail closed: recovered text becomes a live task, so an
                    # unparseable ts would bypass the recency bound it cannot evaluate.
                    break
                candidate = (message.get("text") or "").strip()
                without_mentions = re.sub(
                    r"(?:^|\s)<@[A-Z0-9]+>(?=\s|$)",
                    " ",
                    candidate,
                ).strip()
                if without_mentions:
                    return candidate, True
                # A mention-only prior message is an already-served turn, not one to skip:
                # continuing would reach an older instruction that turn already answered.
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


def _send_reply(channel: str, thread_ts: str | None, text: str, task_id: str | None = None, access_tier: str = "unknown") -> bool:
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

    Returns True when the caller may CONSUME the source (delivered, or there
    was nothing to send) and False when a send was attempted and Slack refused.
    `chat_postMessage` failures are caught in here and recorded as an error
    event rather than raised, so a caller gating cleanup on "did not raise"
    never sees the common failure — it has to consult this value. Purely
    additive: the annotation was `-> None` and no existing caller reads it.
    """
    if not text:
        # Unreachable from every current caller: both the task-reply and
        # proactive drains guard empty text before calling (slack
        # `if not text: claim.unlink()`, telegram :928). Kept as a contract
        # statement rather than removed, because a future caller without that
        # guard must get "consume it" — returning False would release an empty
        # file, re-claim it, and loop forever.
        return True  # pragma: no cover — see above; no caller reaches this

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
            except Exception as e:
                # The deny notice can be the ONLY user-visible output: a body of
                # just `[file: /blocked]` posts no text chunk, so if this refusal
                # is swallowed the caller consumes the source and nobody is told.
                print(f"[Slack] deny-notice chat_postMessage failed: {e}", flush=True)
                delivered_ok = False
            print(f"  BLOCKED file: {fpath}", flush=True)
        else:
            try:
                app.client.chat_postMessage(
                    channel=channel,
                    text=f"(file not found: {fpath})",
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                )
            except Exception as e:
                # Same reasoning as the deny branch above.
                print(f"[Slack] not-found notice chat_postMessage failed: {e}", flush=True)
                delivered_ok = False

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

    return delivered_ok


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
                reply_text = read_ready_result(result_file)
                if reply_text is None:
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
                    if _skip_action.value == "deduped":
                        if _dedup_recover(task_id, _skip_action.extra,
                                          target) == "retain":
                            print(f"  [dedup] report not delivered for {task_id} "
                                  f"— keeping for retry", flush=True)
                            continue
                    print(f"  Skipped (marker): {task_id}", flush=True)
                    # §7 audit ledger: skip-marked results are resolved deliveries
                    # (no_send / deduped), not silent voids. One line per result.
                    _record_skip_audit(task_id, _skip_action.value)
                else:
                    try:
                        delivered = _send_reply(target["channel"], target.get("thread_ts"), reply_text, task_id=task_id, access_tier=target.get("access_tier", "unknown"))
                    except Exception as e:
                        print(f"[Slack] reply error: {e}", flush=True)
                        # Keep both the durable route and result file so the
                        # next poll (or restarted bridge) can retry delivery.
                        continue  # pragma: no cover - watcher loop retry; helper state is unit-tested
                    if not delivered:
                        # Slack refuses without raising, so the except never sees it;
                        # archiving here would destroy an undelivered reply.
                        print(f"[Slack] reply refused, keeping {task_id} for retry", flush=True)
                        continue
                    print(f"  Replied to {target['channel']}: {reply_text[:80]}...", flush=True)

                _pop_pending_reply(task_id)
                archive_file(result_file, "results", task_id)
                archive_file(find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt", "tasks", task_id)

            # Proactive messages (sent to owner DM)
            if not presenter_mode_active(REPO):
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
                    # Peek before claiming: a body addressed to another bridge
                    # is delivered by that bridge, not dumped here as literal text.
                    try:
                        peek = f.read_text(errors="ignore").lstrip()
                    except OSError:
                        continue
                    if not body_claimable_by(peek, "slack"):
                        continue
                    # Explicit filename destination outranks the race.
                    if not _slack_claims_name(f.name):
                        continue
                    # Resolve the owner BEFORE claiming: a claim this bridge
                    # cannot deliver hides the file from the poller that can.
                    try:
                        access_data = json.loads(ACCESS_FILE.read_text())
                    except Exception:
                        access_data = {}
                    owner_id = resolve_proactive_owner_id(access_data)
                    # The shared helper owns "no recipient -> do not claim"; this
                    # adapter contributes only the owner it resolved.
                    claim = claim_for_delivery(f, owner_id)
                    if claim is None:
                        continue
                    text = read_ready_result(claim)
                    if text is None:
                        release_claim(claim)
                        continue
                    if owner_id is not None:
                        # Open a DM channel to the owner (idempotent).
                        try:
                            resp = app.client.conversations_open(users=owner_id)
                            dm_channel = resp["channel"]["id"]
                            if _send_reply(dm_channel, None, text, access_tier="owner"):
                                mark_proactive_delivered(STATE_DIR, delivery_id)
                                print(f"  [proactive] sent to {owner_id}: {text[:80]}", flush=True)
                                claim.unlink(missing_ok=True)
                            else:
                                # Slack refused WITHOUT raising, which is the ordinary
                                # failure; the except below never sees it.
                                print(f"  [proactive] send refused, releasing {claim.name}", flush=True)
                                release_claim(claim)
                        except Exception as e:
                            # Release, never delete: pollers scan `.txt`, so a kept or
                            # deleted claim is a message no bridge can ever retry.
                            print(f"  [proactive] failed, releasing {claim.name}: {e}", flush=True)
                            release_claim(claim)

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
    """Recover this adapter's stranded proactive delivery claims."""
    return recover_orphan_sending_files(RESULTS_DIR)


def main():  # pragma: no cover
    global _TOFU_ENROLLMENT_CODE
    _single_instance_acquire("slack-bridge")
    print("Slack bridge started. Socket Mode connecting...", flush=True)
    _recover_orphan_sending_files()
    # Prime the in-memory access cache so tofu_onboard() can detect external
    # deletions even on the very first inbound message after a restart (#899).
    load_allowed()

    # Seed the tier-map grandfather snapshot at STARTUP, before the first
    # inbound message — otherwise a fresh (pre-migration) install where the
    # owner adds a NEW allowFrom id would grandfather that new id as owner the
    # first time it messages (the on-demand seed captures whoever is in
    # allowFrom at that moment). Seeding at boot pins the snapshot to the
    # allowFrom present at upgrade, so post-upgrade additions default read-only
    # (owner CR #2161). Idempotent: no-op once a tierMap exists.
    _ensure_tier_map_seeded()

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
