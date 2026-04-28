#!/usr/bin/env python3
"""
Telegram bridge for Sutando — polls bot messages, writes to tasks/, sends replies from results/.
Works alongside the voice task bridge. Runs as a background daemon.

Usage: python3 src/telegram-bridge.py
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
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
            os.environ[k.strip()] = v.strip()

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
TASKS_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)


def write_owner_activity(channel: str, summary: str) -> None:
    """Record owner activity — see src/discord-bridge.py for schema."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
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
    try:
        data = json.loads(ACCESS_FILE.read_text())
        return set(data.get("allowFrom", []))
    except:
        return set()

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

def send_reply(chat_id, text):
    import re
    # Extract file paths: [file: /path/to/file] or [send: /path/to/file]
    file_pattern = re.compile(r'\[(?:file|send|attach):\s*([^\]]+)\]')
    files = file_pattern.findall(text)
    clean_text = file_pattern.sub('', text).strip()

    # Send text (if any remains after extracting file refs)
    if clean_text:
        for i in range(0, len(clean_text), 4000):
            api("sendMessage", chat_id=chat_id, text=clean_text[i:i+4000])

    # Send files (allowlist-gated; see _is_path_sendable)
    for fpath in files:
        fpath = fpath.strip()
        if _is_path_sendable(fpath):
            send_file(chat_id, fpath)
            print(f"  Sent file: {fpath}")
        elif os.path.isfile(fpath):
            api("sendMessage", chat_id=chat_id, text=f"(file access denied: {fpath})")
            print(f"  BLOCKED file: {fpath}")
        else:
            api("sendMessage", chat_id=chat_id, text=f"(file not found: {fpath})")

def main():
    print(f"Telegram bridge started. Polling for messages...", flush=True)
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
                        attachment_note = f"\n[Voice note attached: {local_path}]"

                if not text and not attachment_note:
                    continue

                print(f"  @{username}: {text}{attachment_note}")

                # Write as task (same format as voice bridge)
                ts = int(time.time() * 1000)
                task_id = f"task-{ts}"
                task_file = TASKS_DIR / f"{task_id}.txt"
                task_file.write_text(
                    f"id: {task_id}\n"
                    f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S')}Z\n"
                    f"task: [Telegram @{username}] {text}{attachment_note}\n"
                    f"source: telegram\n"
                    f"chat_id: {chat_id}\n"
                )
                pending_replies[task_id] = chat_id

                # Send typing indicator
                api("sendChatAction", chat_id=chat_id, action="typing")

        # Check for proactive messages to send to owner.
        # Presenter-mode: retain files (don't unlink, don't send) so they
        # flush after the talk window ends. See presenter-mode.sh contract.
        try:
            if not presenter_mode_active():
                for f in RESULTS_DIR.iterdir():
                    if f.name.startswith("proactive-") and f.suffix == ".txt":
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
                        owner_ids = load_allowed()
                        if owner_ids:
                            owner_id = next(iter(owner_ids))
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
                # Skip sending if already replied directly.
                # Clean up both files so watcher doesn't re-fire on leftover
                # task — same bug class as discord-bridge had.
                if reply_text.startswith('[no-send]') or reply_text.startswith('[REPLIED]'):
                    print(f"  Skipped (already replied): {task_id}")
                    archive_file(result_file, "results", task_id)
                    task_file = TASKS_DIR / f"{task_id}.txt"
                    archive_file(task_file, "tasks", task_id)
                    continue
                try:
                    send_reply(chat_id, reply_text)
                    print(f"  Replied to {chat_id}: {reply_text[:80]}...", flush=True)
                except Exception as e:
                    print(f"[Telegram] Reply error: {e}", flush=True)
                # Archive (not delete) so we can mine patterns later.
                archive_file(result_file, "results", task_id)
                task_file = TASKS_DIR / f"{task_id}.txt"
                archive_file(task_file, "tasks", task_id)

        time.sleep(1)

if __name__ == "__main__":
    main()
