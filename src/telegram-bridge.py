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
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

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
from result_markers import parse_markers  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402
from task_archive import find_task_file  # noqa: E402
from single_instance import acquire as _single_instance_acquire  # noqa: E402
from vault_intercept import intercept_vault_commands, redact_vault_commands  # noqa: E402
REPO = resolve_workspace()
TASKS_DIR = REPO / "tasks"
RESULTS_DIR = REPO / "results"

# Allowlist for paths that may be sent via Telegram [file: /path] markers.
# Mirrors _is_path_sendable() in discord-bridge.py.
SEND_ALLOWED_ROOTS = (
    str(REPO / "results"),
    str(REPO / "notes"),
    str(REPO / "docs"),
)
SEND_ALLOWED_PREFIXES = (
    "/tmp/sutando-",
    "/private/tmp/sutando-",
    "/tmp/echo-",
    "/private/tmp/echo-",
)


def _is_path_sendable(fpath: str) -> bool:
    """True iff `fpath` is a real file AND resolves under an allowed root."""
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
channels_env = Path.home() / ".claude" / "channels" / "telegram" / ".env"
if channels_env.exists():
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
    print("TELEGRAM_BOT_TOKEN not set")
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


def write_owner_activity(channel: str, summary: str) -> None:
    """Record owner activity — see src/discord-bridge.py for schema."""
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
        print(f"  [owner-activity] write failed: {e}")


def archive_file(src: "Path", kind: str, task_id: str) -> None:
    """Move src into archive/<tasks|results>/YYYY-MM/ instead of deleting.
    Silent on failure. Chi's ask 2026-04-18: archive tasks + results for
    later pattern-mining / self-improvement analysis."""
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
        print(f"[Telegram] archive_file({kind}, {task_id}) failed: {e}")
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass

# Presenter mode: silence proactive DMs during ICLR/talk windows. Sentinel
# is written by scripts/presenter-mode.sh with an ISO-8601 expiry. Matches
# the check in src/check-pending-questions.py and src/discord-bridge.py.
PRESENTER_SENTINEL = REPO / "state" / "presenter-mode.sentinel"


def presenter_mode_active():
    if not PRESENTER_SENTINEL.exists():
        return False
    try:
        expire_iso = PRESENTER_SENTINEL.read_text().strip()
        # Require an ISO-8601-ish prefix (starts with a digit). Without
        # this guard, malformed sentinel content like "garbage" compares
        # LESS than any real now_iso ("2" < "g" in ASCII) and the mode
        # fails OPEN — appears active forever.
        if not expire_iso or not expire_iso[0].isdigit():
            return False
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return now_iso < expire_iso
    except Exception:
        return False

# Load access config
ACCESS_FILE = Path.home() / ".claude" / "channels" / "telegram" / "access.json"
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
    ACCESS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(ACCESS_FILE, 0o600)  # don't inherit umask 644 — file holds owner's Telegram user ID
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
    import subprocess
    skill_script = Path(__file__).parent.parent / "skills" / "audio-transcribe" / "scripts" / "transcribe.py"
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
        urllib.request.urlretrieve(url, str(local_path))
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

def send_reply(chat_id, text, task_id: str | None = None):
    # Extract file paths: [file: /path/to/file] or [send: /path/to/file]
    file_pattern = re.compile(r'\[(?:file|send|attach):\s*([^\]]+)\]')
    files = file_pattern.findall(text)
    clean_text = file_pattern.sub('', text).strip()

    # Send text (if any remains after extracting file refs)
    if clean_text:
        for i in range(0, len(clean_text), 4000):
            api("sendMessage", chat_id=chat_id, text=clean_text[i:i+4000])
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
            send_file(chat_id, fpath)
            print(f"  Sent file: {fpath}", flush=True)
        elif os.path.isfile(fpath):
            api("sendMessage", chat_id=chat_id, text=f"(file access denied: {fpath})")
            print(f"  BLOCKED file: {fpath}")
        else:
            # Prose-quoted `[file:/path]` substrings extract as markers
            # but reference no actual file. Don't ship the warning to
            # the user; log for operator visibility on real typos. Same
            # rationale as discord-bridge:poll_results.
            print(f"  file marker, file not found — likely a prose quotation: {fpath}", flush=True)

def _recover_orphan_sending_files() -> int:
    """Restart-safety: rename any orphan `results/proactive-*.sending`
    files back to `*.txt` so they get re-claimed on the next poll.

    Mirrors `_recover_orphan_sending_files` in discord-bridge.py.
    See that docstring for the bug class this closes.
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
            pass
        except Exception as e:
            print(f"  [startup] failed to recover {f.name}: {e}", flush=True)
    if recovered:
        print(f"  [startup] recovered {recovered} orphan .sending file(s)", flush=True)
    return recovered


def main():
    _single_instance_acquire("telegram-bridge")
    print(f"Telegram bridge started. Polling for messages...", flush=True)
    # Restart-safety: sweep orphan `.sending` files before the poll
    # loop starts. See _recover_orphan_sending_files for rationale.
    _recover_orphan_sending_files()
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
                text = msg.get("text", "")

                # Reload access list periodically
                allowed = load_allowed()
                if allowed is None:
                    # First-ever DM after install — access.json doesn't exist.
                    # Auto-onboard this sender as the owner (TOFU).
                    allowed = tofu_onboard(sender_id, username)
                if sender_id not in allowed:
                    print(f"  Dropped message from non-allowed @{username}")
                    continue

                # Record owner activity for status-aware-pivot
                write_owner_activity("telegram", text)

                # Handle attachments (photos, documents, voice)
                attachment_note = ""
                if "photo" in msg:
                    file_id = msg["photo"][-1]["file_id"]  # largest size
                    local_path = download_file(file_id, "photo")
                    if local_path:
                        attachment_note = f"\n[Photo attached: {local_path}]"
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
                if "voice" in msg:
                    file_id = msg["voice"]["file_id"]
                    local_path = download_file(file_id, "voice.ogg")
                    if local_path:
                        transcript = _transcribe_via_skill(local_path)
                        if transcript:
                            attachment_note = f"\n[Voice transcript: {transcript}]"
                        else:
                            attachment_note = f"\n[Voice note attached: {local_path}]"

                if not text and not attachment_note:
                    continue

                forward_note = extract_forward_note(msg)

                print(f"  @{username}{forward_note}: {redact_vault_commands(text)}{attachment_note}")

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

                # Inject skill instructions so the agent follows notify-before-work
                # and transcription protocol even after conversation compaction.
                # Only injected when the referenced skills are installed on this node.
                # CCD-resolved (PR #1525 pattern): never hardcode ~/.claude — nodes may relocate
                # the config dir via $CLAUDE_CONFIG_DIR.
                _claude_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
                _notify_py = _claude_config / "skills/task-progress/scripts/notify.py"
                _transcribe_py = _claude_config / "skills/audio-transcribe/scripts/transcribe.py"
                has_audio_attach = attachment_note and any(
                    attachment_note.lower().find(ext) != -1
                    for ext in (".m4a", ".mp3", ".ogg", ".opus", ".oga", ".wav", ".webm", ".aac")
                )
                tg_skill_hints = ""
                if _notify_py.exists() or _transcribe_py.exists():
                    lines = ["===SKILL INSTRUCTIONS (follow before any other action)==="]
                    step = 1
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

                task_file.write_text(
                    f"id: {task_id}\n"
                    f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                    f"task: [Telegram @{username}{forward_note}] {text}{attachment_note}\n"
                    f"source: telegram\n"
                    f"chat_id: {chat_id}\n"
                    f"priority: {priority}\n"
                    f"{tg_skill_hints}"
                )
                pending_replies[task_id] = chat_id

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
        from proactive_routing import should_claim_proactive
        try:
            if (
                not presenter_mode_active()
                and should_claim_proactive(OWNER_ACTIVITY_FILE, "telegram")
            ):
                # discord-bridge.poll_dm_fallback handles briefing-/insight-/
                # friction-*.txt via FALLBACK_PREFIXES; telegram-bridge only
                # matched `proactive-`, so morning-briefing output (which
                # writes `results/briefing-{date}.txt` per the skill
                # contract) was silently archived without reaching Telegram.
                # Treat the same prefixes as proactive-equivalent so
                # cron-originated results land in the owner's DM regardless
                # of which bridge is the active channel.
                PROACTIVE_PREFIXES = ("proactive-", "briefing-", "insight-", "friction-")
                for f in RESULTS_DIR.iterdir():
                    if any(f.name.startswith(p) for p in PROACTIVE_PREFIXES) and f.suffix == ".txt":
                        # Peek before claiming: skip Discord-targeted proactive files.
                        # [channel: <17-20 digit snowflake>] is a Discord-only marker;
                        # claiming it here sends the literal text to Telegram DM instead
                        # of leaving it for discord-bridge. (#1401)
                        try:
                            peek = f.read_text(errors="ignore").lstrip()
                        except OSError:
                            continue
                        if peek.startswith("[channel:") and \
                                re.match(r'\[channel:\s*\d{17,20}\]', peek):
                            continue
                        # Claim-by-rename: atomic move to a `.sending`
                        # suffix before reading, so a concurrent poll
                        # (same bridge, or a race with discord-bridge)
                        # can't pick it up and resend. See
                        # discord-bridge.py for the same fix + the
                        # 2026-04-20 bug-scenario that motivated it.
                        claim = f.with_suffix(".sending")
                        try:
                            f.rename(claim)
                        except FileNotFoundError:
                            continue
                        f = claim
                        text = f.read_text().strip()
                        if not text:
                            f.unlink(missing_ok=True)
                            continue
                        # Pre-fix used `next(iter(load_allowed()))`,
                        # which iterates a `set` — hash-slot order, not
                        # list order. With multiple users in allowFrom
                        # (e.g. admin adds a second sender via
                        # `/telegram:access allow`), proactive
                        # owner-notifications could route to the wrong
                        # user. Mirrors the same fix shape used by
                        # discord-bridge's poll_proactive; full priority
                        # chain documented on _resolve_proactive_owner_id.
                        env_override = os.environ.get("SUTANDO_DM_OWNER_ID", "").strip()
                        try:
                            access_data = json.loads(ACCESS_FILE.read_text())
                        except Exception:
                            access_data = {}
                        owner_id = _resolve_proactive_owner_id(env_override, access_data)
                        if owner_id is None:
                            print(f"  [proactive] no owner in allowFrom, skipping {f.name}")
                            f.unlink(missing_ok=True)
                            continue
                        try:
                            send_reply(int(owner_id), text)
                            print(f"  [proactive] sent to {owner_id}: {text[:80]}")
                        except Exception as e:
                            print(f"  [proactive] failed: {e}")
                        f.unlink(missing_ok=True)
        except Exception as e:
            print(f"  [proactive] poll error: {e}")

        # Check for results to send back
        for task_id in list(pending_replies.keys()):
            result_file = RESULTS_DIR / f"{task_id}.txt"
            if result_file.exists():
                reply_text = result_file.read_text().strip()
                chat_id = pending_replies.pop(task_id)
                # Parse markers via the unified module (#873). Telegram
                # honors [no-send] / [REPLIED] / [deduped: <id>] as skip,
                # sends attached files, and silently drops [channel:] redirects
                # (no concept in Telegram). Pass parsed.body so NO marker ever
                # leaks as literal text in the user's DM (#1381).
                parsed = parse_markers(reply_text)
                if any(a.kind == "skip" for a in parsed.actions):
                    print(f"  Skipped (marker): {task_id}", flush=True)
                    archive_file(result_file, "results", task_id)
                    task_file = find_task_file(TASKS_DIR, task_id) or TASKS_DIR / f"{task_id}.txt"
                    archive_file(task_file, "tasks", task_id)
                    continue
                try:
                    # Use parsed.body — all markers stripped — so [channel:] etc. never leak.
                    # File attachments are in parsed.actions; send_reply() won't re-find them.
                    send_reply(chat_id, parsed.body, task_id=task_id)
                    for action in parsed.actions:
                        if action.kind == "attach":
                            fpath = action.value.strip()
                            if _is_path_sendable(fpath):
                                send_file(chat_id, fpath)
                                print(f"  Sent file: {fpath}", flush=True)
                            elif os.path.isfile(fpath):
                                api("sendMessage", chat_id=chat_id, text=f"(file access denied: {fpath})")
                                print(f"  BLOCKED file: {fpath}")
                            else:
                                print(f"  file marker, file not found — likely a prose quotation: {fpath}", flush=True)
                    print(f"  Replied to {chat_id}: {parsed.body[:80]}...", flush=True)
                except Exception as e:
                    print(f"[Telegram] Reply error: {e}", flush=True)
                # Archive (not delete) so we can mine patterns later.
                archive_file(result_file, "results", task_id)
                task_file = TASKS_DIR / f"{task_id}.txt"
                archive_file(task_file, "tasks", task_id)

        time.sleep(1)

if __name__ == "__main__":
    main()
