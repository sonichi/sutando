#!/usr/bin/env python3
"""Post one browser step into the task's room: a short line, optionally with
a screenshot of the page as it is right now.

Usage:
    python3 step.py --source ag2space --channel-id '!room:server' \
        --message "Opened the checkout page — 2 items, $84.10" \
        --screenshot                       # capture --url now via src/browser.mjs
    python3 step.py ... --message "Filled the shipping form" --screenshot /path/to/shot.png
    python3 step.py ... --message "Searching flights" --url https://example.com --screenshot

Why this exists (owner feedback, 2026-09-20): a browsing task that reports
only "done" hides what the agent saw and did; the person wants each step in
the chat, text and picture, and a screenshot before anything is bought or
submitted. The line goes through notify.py's gateway sender (same worker
stamp, same length rule); the image goes through the gateway's
POST /v1/rooms/<room>/media, the route `[file:]` markers already take, under
the same allowlist. Screenshots from src/browser.mjs land in
$SUTANDO_SCREENSHOT_DIR (default <tmpdir>/sutando-screenshots), which is
admitted here as this script's own extra root — nothing else may send from
there.

Exit 0 when the text step was posted (a failed screenshot is a warning: the
line is the load-bearing part, and a step must never block the task). Exit 1
when the text could not be sent or was refused (too long: this is a step,
not a report).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
BROWSER_MJS = _REPO / "src" / "browser.mjs"
CAPTURE_TIMEOUT_S = 90


def screenshot_dir() -> str:
    """Where src/browser.mjs writes (same env knob, same default)."""
    return os.environ.get("SUTANDO_SCREENSHOT_DIR") or os.path.join(
        tempfile.gettempdir(), "sutando-screenshots")


def capture(url: str) -> "tuple[str, str]":
    """Full-page screenshot of `url` through src/browser.mjs. (path, "") or
    ("", reason). The last stdout line is the path browser.mjs printed."""
    if not url:
        return "", "no --url to capture (pass --screenshot <path> for an existing image)"
    try:
        proc = subprocess.run(
            ["node", str(BROWSER_MJS), url, "screenshot", "--timeout=60000"],
            capture_output=True, text=True, timeout=CAPTURE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"capture failed: {e}"
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        err = (proc.stderr or "").strip().splitlines()
        return "", f"capture failed: {err[-1] if err else f'exit {proc.returncode}'}"
    path = lines[-1]
    if not os.path.isfile(path):
        return "", f"capture returned no file: {path}"
    return path, ""


def send_text(source: str, channel: str, message: str, thread_ts: str | None) -> bool:
    if source == "slack":
        return notify.send_slack(channel, message, thread_ts=thread_ts)
    if source == "discord":
        return notify.send_discord(channel, message)
    if source == "telegram":
        return notify.send_telegram(channel, message)
    return notify.send_remote_gateway(source, channel, message)


def run(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Post one browser step (text + optional screenshot).")
    parser.add_argument("--source", required=True)
    parser.add_argument("--channel-id", help="Room / channel id")
    parser.add_argument("--chat-id", help="Telegram chat id (alias)")
    parser.add_argument("--thread-ts", default=None, help="Slack thread timestamp")
    parser.add_argument("--message", required=True, help="One short line: what you just did / see")
    parser.add_argument("--url", default=None, help="Page to capture when --screenshot has no path")
    parser.add_argument("--screenshot", nargs="?", const="", default=None,
                        help="Attach a screenshot: a path, or bare to capture --url now")
    args = parser.parse_args(argv)

    channel = args.channel_id or args.chat_id
    if not channel:
        print("[task-progress] --channel-id (or --chat-id) is required", file=sys.stderr)
        return 1
    err = notify._progress_message_error(args.message)
    if err:
        print(f"[task-progress] refusing step: {err}. A step is one short line; "
              "the report goes in the task result file.", file=sys.stderr)
        return 1

    if not send_text(args.source, channel, args.message, args.thread_ts):
        print("[task-progress] step text not sent", file=sys.stderr)
        return 1

    if args.screenshot is None:
        return 0
    path = args.screenshot
    if not path:
        path, reason = capture(args.url or "")
        if not path:
            print(f"[task-progress] screenshot skipped: {reason}", file=sys.stderr)
            return 0
    ok, reason = notify.upload_room_media(args.source, channel, path,
                                         extra_roots=(screenshot_dir(),))
    if not ok:
        print(f"[task-progress] screenshot not posted: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
