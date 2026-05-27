#!/usr/bin/env python3
"""EA Scheduling — detect inbound meeting requests and draft availability replies.

Sutando acts as executive assistant: finds meeting request emails,
checks Google Calendar for free slots, drafts a reply with 3 options.
Dry-run by default — pass --send to actually reply and label the thread.

Usage:
    python3 skills/ea-scheduling/scripts/handle-meeting-requests.py [--send] [--days N]

Options:
    --send      Actually send replies and apply EA-Handled Gmail label (default: dry-run)
    --days N    Look ahead N days for availability (default: 7)
    --limit N   Max requests to process in one pass (default: 5)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EA_HANDLED_LABEL = "EA-Scheduled"
DEDUP_STATE = Path(__file__).resolve().parent.parent / "state" / "ea-handled-ids.json"
SLOT_COUNT = 3          # number of availability slots to offer
SLOT_DURATION = 30      # default meeting duration in minutes
WORK_START = 9          # 9 AM (owner's timezone — assumes WORKSPACE_DIR/.env has TZ)
WORK_END = 18           # 6 PM

# Keywords that identify a scheduling / meeting request email.
REQUEST_PATTERNS = [
    r"(?i)\b(schedule|book|set up|arrange|find time|grab time|meet)\b.{0,40}\b(meeting|call|chat|sync|intro|30 min|hour|week|time)\b",
    r"(?i)\b(available|availability|free|open)\b.{0,40}\b(meeting|call|chat|time|slot|week|day)\b",
    r"(?i)\b(would love to|like to|hoping to|keen to|interested in)\b.{0,40}\b(chat|connect|meet|speak|talk)\b",
    r"(?i)\b(calendly|calendar\.com)\b",
    r"(?i)\bschedul(e|ing)\b.{0,60}\b(time|you|connect|discuss)\b",
]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


# ---------------------------------------------------------------------------
# Dedup state — skip emails already handled in a prior pass
# ---------------------------------------------------------------------------

def _load_handled() -> set[str]:
    DEDUP_STATE.parent.mkdir(parents=True, exist_ok=True)
    if DEDUP_STATE.exists():
        try:
            return set(json.loads(DEDUP_STATE.read_text()))
        except Exception:
            pass
    return set()


def _save_handled(ids: set[str]) -> None:
    DEDUP_STATE.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_STATE.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def find_meeting_requests(limit: int) -> list[dict]:
    """Search Gmail inbox for unread meeting request threads."""
    query = "in:inbox is:unread -from:me -label:EA-Scheduled"
    result = _run(["gws", "gmail", "users", "messages", "list",
                   "--params", f"q={query} newer_than:14d maxResults=50"])
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    messages = data.get("messages", [])
    handled = _load_handled()
    candidates = []

    for msg in messages:
        msg_id = msg.get("id", "")
        if msg_id in handled:
            continue
        # Fetch full message to inspect body
        detail = _run(["gws", "gmail", "users", "messages", "get",
                       "--id", msg_id, "--format", "full"])
        if detail.returncode != 0:
            continue
        try:
            mdata = json.loads(detail.stdout)
        except json.JSONDecodeError:
            continue

        snippet = mdata.get("snippet", "")
        subject = _extract_header(mdata, "Subject")
        sender = _extract_header(mdata, "From")
        thread_id = mdata.get("threadId", msg_id)

        # Check if it looks like a meeting request
        text = f"{subject} {snippet}"
        if _is_meeting_request(text):
            candidates.append({
                "id": msg_id,
                "thread_id": thread_id,
                "subject": subject,
                "from": sender,
                "snippet": snippet[:200],
            })
        if len(candidates) >= limit:
            break

    return candidates


def _extract_header(msg: dict, name: str) -> str:
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _is_meeting_request(text: str) -> bool:
    return any(re.search(p, text) for p in REQUEST_PATTERNS)


def get_sender_email(from_header: str) -> str:
    """Extract plain email from 'Name <email>' format."""
    m = re.search(r"<([^>]+)>", from_header)
    return m.group(1) if m else from_header.strip()


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def get_busy_blocks(days: int) -> list[tuple[datetime, datetime]]:
    """Return list of (start, end) busy blocks from Google Calendar."""
    result = _run(["gws", "calendar", "+agenda", f"--days", str(days), "--format", "json"])
    if result.returncode != 0:
        return []
    try:
        events = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    blocks = []
    for ev in events if isinstance(events, list) else events.get("items", []):
        start_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        end_str = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        if not start_str or not end_str:
            continue
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            blocks.append((start, end))
        except ValueError:
            continue
    return blocks


def find_available_slots(days: int, duration_min: int, count: int) -> list[tuple[datetime, datetime]]:
    """Return up to `count` available slots in the next `days` days."""
    busy = get_busy_blocks(days)
    slots = []
    now = datetime.now(tz=timezone.utc)
    # Start search from next working hour
    candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while len(slots) < count and candidate < now + timedelta(days=days):
        # Skip weekends
        if candidate.weekday() >= 5:
            candidate += timedelta(days=1)
            candidate = candidate.replace(hour=WORK_START, minute=0)
            continue
        # Local hour approximation (Dubai UTC+4)
        local_hour = (candidate.hour + 4) % 24
        if local_hour < WORK_START:
            candidate = candidate.replace(hour=WORK_START - 4)  # adjust to UTC equivalent
            continue
        if local_hour >= WORK_END:
            candidate += timedelta(days=1)
            candidate = candidate.replace(hour=WORK_START - 4, minute=0)
            continue

        end_candidate = candidate + timedelta(minutes=duration_min)
        # Check no busy overlap
        overlap = any(
            not (end_candidate <= b_start or candidate >= b_end)
            for b_start, b_end in busy
        )
        if not overlap:
            slots.append((candidate, end_candidate))
            candidate = end_candidate
        else:
            candidate += timedelta(minutes=30)

    return slots


def format_slot(start: datetime, end: datetime) -> str:
    """Format a slot as 'Tuesday 27 May, 10:00–10:30 AM (Dubai, UTC+4)'."""
    local = start + timedelta(hours=4)  # UAE time
    day = local.strftime("%A %d %B")
    t_start = local.strftime("%I:%M %p").lstrip("0")
    local_end = end + timedelta(hours=4)
    t_end = local_end.strftime("%I:%M %p").lstrip("0")
    return f"{day}, {t_start}–{t_end} (Dubai, UTC+4)"


# ---------------------------------------------------------------------------
# Reply drafting
# ---------------------------------------------------------------------------

def draft_reply(request: dict, slots: list[tuple[datetime, datetime]]) -> str:
    sender_name = request["from"].split("<")[0].strip().split()[0] or "there"
    subject = request["subject"]

    slot_lines = "\n".join(
        f"  {i+1}. {format_slot(s, e)}" for i, (s, e) in enumerate(slots)
    ) if slots else "  (No availability found — please propose a time that works for you.)"

    return f"""Hi {sender_name},

Thanks for reaching out! I'd be happy to connect.

Here are a few times that work on my end:

{slot_lines}

Does any of these work for you? If not, feel free to share your availability and I'll do my best to make it work.

Looking forward to speaking,
Bassil"""


# ---------------------------------------------------------------------------
# Gmail send + label
# ---------------------------------------------------------------------------

def send_reply(thread_id: str, to_email: str, subject: str, body: str, dry_run: bool) -> bool:
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if dry_run:
        print(f"  [DRY-RUN] Would reply to {to_email}")
        print(f"  Subject: {reply_subject}")
        print(f"  Body preview: {body[:120].replace(chr(10), ' ')}...")
        return True
    result = _run(["gws", "gmail", "+send",
                   "--to", to_email,
                   "--subject", reply_subject,
                   "--body", body])
    return result.returncode == 0


def apply_label(message_id: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] Would label message {message_id} with {EA_HANDLED_LABEL!r}")
        return
    # Create label if needed, then apply
    _run(["gws", "gmail", "users", "labels", "create",
          "--body", json.dumps({"name": EA_HANDLED_LABEL})])
    _run(["gws", "gmail", "users", "messages", "modify",
          "--id", message_id,
          "--body", json.dumps({"addLabelIds": [], "removeLabelIds": []})])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Actually send replies (default: dry-run)")
    parser.add_argument("--days", type=int, default=7, help="Days ahead to search for availability")
    parser.add_argument("--limit", type=int, default=5, help="Max requests to process")
    args = parser.parse_args()

    dry_run = not args.send

    if dry_run:
        print("EA Scheduling — DRY RUN (pass --send to actually reply)\n")

    requests = find_meeting_requests(args.limit)
    if not requests:
        print("No unhandled meeting requests found.")
        return 0

    print(f"Found {len(requests)} meeting request(s).\n")
    handled = _load_handled()
    new_handled = set()

    slots = find_available_slots(args.days, SLOT_DURATION, SLOT_COUNT)
    if not slots and not dry_run:
        print("Warning: no available calendar slots found — replies will ask sender to propose times.")

    for req in requests:
        sender_email = get_sender_email(req["from"])
        print(f"→ From: {req['from']}")
        print(f"  Subject: {req['subject']}")
        print(f"  Snippet: {req['snippet'][:100]}...")

        body = draft_reply(req, slots)
        ok = send_reply(req["thread_id"], sender_email, req["subject"], body, dry_run)
        if ok:
            apply_label(req["id"], dry_run)
            new_handled.add(req["id"])
            print(f"  ✓ {'Queued (dry-run)' if dry_run else 'Replied + labeled'}\n")
        else:
            print(f"  ✗ Failed to send reply\n")

    if not dry_run:
        _save_handled(handled | new_handled)

    return 0


if __name__ == "__main__":
    sys.exit(main())
