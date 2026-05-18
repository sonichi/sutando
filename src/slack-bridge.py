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
    channels:history, groups:history

Access list (TOFU onboarding, same schema as telegram):
    ~/.claude/channels/slack/access.json
        {"allowFrom": ["U0123..."], "tofuOwner": "U0123...", ...}

File attachments are NOT supported in v0 — see issue #866 for the full scope.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_priority import default_priority_for_source  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

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
ARCHIVE_TASKS_DIR = REPO / "tasks" / "archive"
ARCHIVE_RESULTS_DIR = REPO / "results" / "archive"
OWNER_ACTIVITY_FILE = STATE_DIR / "last-owner-activity.json"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
if not BOT_TOKEN or not APP_TOKEN:
    print("SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN not set", file=sys.stderr)
    sys.exit(1)


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


def load_allowed():
    """Return set of allowed Slack user IDs, or None if access.json missing.

    None vs empty-set: file-missing means never-configured (TOFU-eligible);
    empty allowFrom means admin explicitly locked it down (no TOFU)."""
    try:
        data = json.loads(ACCESS_FILE.read_text())
        return set(data.get("allowFrom", []))
    except FileNotFoundError:
        return None
    except Exception:
        return set()


def tofu_onboard(user_id: str, username: str | None) -> set:
    """First-time auto-onboard — same contract as telegram-bridge.py."""
    if ACCESS_FILE.exists():
        return load_allowed() or set()
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "allowFrom": [user_id],
        "tofuOwner": user_id,
        "tofuOnboardedAt": int(time.time()),
        "tofuOnboardedUsername": username or None,
    }
    ACCESS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(ACCESS_FILE, 0o600)
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

# Bolt App. Socket Mode handler attaches via SocketModeHandler below.
app = App(token=BOT_TOKEN)


def _write_task(event: dict, prefix: str, text: str, username: str | None) -> str | None:
    """Write a task file from a Slack event. Returns task_id or None if skipped."""
    user_id = event.get("user")
    if not user_id:
        return None

    # Access control via TOFU
    allowed = load_allowed()
    if allowed is None:
        allowed = tofu_onboard(user_id, username)
    if user_id not in allowed:
        print(f"  Dropped message from non-allowed user {user_id}", flush=True)
        return None

    write_owner_activity("slack", text)

    channel = event.get("channel", "")
    # Reply in-thread for channel @mentions, top-level for DMs.
    thread_ts = event.get("thread_ts") or event.get("ts") if event.get("channel_type") != "im" else None

    ts = int(time.time() * 1000)
    task_id = f"task-{ts}"
    task_file = TASKS_DIR / f"{task_id}.txt"
    priority = default_priority_for_source("slack", "owner")
    task_file.write_text(
        f"id: {task_id}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S')}Z\n"
        f"task: [{prefix} @{username or user_id}] {text}\n"
        f"source: slack\n"
        f"channel_id: {channel}\n"
        f"user_id: {user_id}\n"
        f"access_tier: owner\n"
        f"priority: {priority}\n"
    )
    with pending_replies_lock:
        pending_replies[task_id] = {"channel": channel, "thread_ts": thread_ts}

    print(f"  Wrote {task_id} from {prefix} @{username}", flush=True)
    return task_id


def _resolve_username(user_id: str) -> str | None:
    try:
        resp = app.client.users_info(user=user_id)
        return resp["user"]["profile"].get("display_name") or resp["user"].get("name")
    except Exception:
        return None


@app.event("app_mention")
def handle_mention(event, say):
    """Channel @mention → task file."""
    user_id = event.get("user")
    username = _resolve_username(user_id) if user_id else None
    # Strip the leading <@BOTID> mention from the text body for cleanliness.
    raw = event.get("text", "")
    import re
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", raw).strip()
    if not text:
        return
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
    text = event.get("text", "").strip()
    if not text:
        return
    _write_task(event, "Slack DM", text, username)


def _send_reply(channel: str, thread_ts: str | None, text: str) -> None:
    """Post a reply via chat.postMessage. Slack truncates messages at ~40KB
    but Discord-style 2000-char chunking keeps things readable in the UI."""
    if not text:
        return
    for i in range(0, len(text), 4000):
        kwargs = {"channel": channel, "text": text[i:i + 4000]}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            app.client.chat_postMessage(**kwargs)
        except Exception as e:
            print(f"[Slack] chat_postMessage failed: {e}", flush=True)
            break


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

                if (reply_text.startswith("[no-send]")
                        or reply_text.startswith("[REPLIED]")
                        or reply_text.startswith("[deduped:")):
                    print(f"  Skipped (marker): {task_id}", flush=True)
                else:
                    try:
                        _send_reply(target["channel"], target.get("thread_ts"), reply_text)
                        print(f"  Replied to {target['channel']}: {reply_text[:80]}...", flush=True)
                    except Exception as e:
                        print(f"[Slack] reply error: {e}", flush=True)

                archive_file(result_file, "results", task_id)
                archive_file(TASKS_DIR / f"{task_id}.txt", "tasks", task_id)

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


def main():
    print("Slack bridge started. Socket Mode connecting...", flush=True)
    threading.Thread(target=result_watcher, name="slack-result-watcher", daemon=True).start()
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()  # blocks


if __name__ == "__main__":
    main()
