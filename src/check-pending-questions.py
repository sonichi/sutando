#!/usr/bin/env python3
"""Check pending questions and notify if unanswered.

Runs on cron — independent of the proactive loop.
Sends notifications via macOS + Discord DM if questions are waiting.
Use --force to bypass the 1-hour cooldown.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import personal_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
PQ_FILE = Path(personal_path("pending-questions.md", WORKSPACE))
RESULTS_DIR = WORKSPACE / "results"
LAST_NOTIFY_FILE = WORKSPACE / ".last-pq-notify"
VOICE_LOG = WORKSPACE / "logs" / "voice-agent.log"
PRESENTER_SENTINEL = WORKSPACE / "state" / "presenter-mode.sentinel"


def presenter_mode_active():
    """True if scripts/presenter-mode.sh has been started and the expiry
    timestamp in the sentinel is still in the future. Silences all
    notifications for the ICLR talk window. Stale sentinels (past-expiry)
    are ignored and return False — the next `status` / `stop` call will
    remove the file."""
    if not PRESENTER_SENTINEL.exists():
        return False
    try:
        expire_iso = PRESENTER_SENTINEL.read_text().strip()
        # Require an ISO-8601-ish prefix (starts with a digit). Without
        # this guard, malformed sentinel content like "garbage" compares
        # LESS than any real now_iso ("2" < "g" in ASCII) and the mode
        # fails OPEN — appears active forever. The same guard is in
        # src/discord-bridge.py and src/telegram-bridge.py.
        if not expire_iso or not expire_iso[0].isdigit():
            return False
        # Compare as ISO-8601 with Z suffix — sorts correctly as strings.
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return now_iso < expire_iso
    except Exception:
        return False


def voice_client_connected():
    """True if the most recent [Health] line in voice-agent.log shows client=true.
    When the voice client is offline, dm-fallback already delivers question-*.txt
    files via Discord DM — writing one would double-DM with notify_discord_dm."""
    if not VOICE_LOG.exists():
        return False
    try:
        # Read the tail efficiently: open at end, walk back ~16KB
        with VOICE_LOG.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode('utf-8', errors='replace')
        for line in reversed(tail.splitlines()):
            if '[Health]' in line and 'client=' in line:
                return 'client=true' in line
    except Exception:
        pass
    return False


def get_waiting_questions():
    """Parse pending-questions.md — matches the legacy `## Q1 — Title` and
    `## Title` / `- **Status:** unanswered` section formats AND the free-form
    `- **[label, ts]** ...` bullet format the proactive-loop writes in practice.

    If a section has no explicit **Status:** marker, it is treated as
    unanswered (the free-form prose format used in practice never writes
    a status field; sections are deleted when resolved, not marked done).
    Sections with an explicit status of "resolved" / "done" / "answered"
    are skipped so the old structured format still works correctly.
    """
    if not PQ_FILE.exists():
        return []
    content = PQ_FILE.read_text()
    # Only the active region counts. Resolved questions are kept below a
    # top-level "# Resolved" divider (audit trail), not deleted — without
    # this cut the heading-agnostic split below sweeps the whole file and
    # every resolved entry is miscounted as pending, re-notifying the owner
    # about already-answered questions. No-op when there is no such divider.
    content = re.split(r'^#\s+Resolved\b', content, maxsplit=1, flags=re.MULTILINE)[0]
    questions = []
    # Walk each ## section; a section is waiting if its body contains
    # `Status: unanswered` or `Status: Waiting`, OR has no Status field
    # at all (free-form prose sections are always unanswered by convention).
    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    for sec in sections[1:]:  # skip pre-header
        title_line, _, body = sec.partition('\n')
        title = title_line.strip()
        if not title:
            continue
        status_m = re.search(r'\*\*Status:\*\*\s*(.+)', body)
        if status_m:
            status = status_m.group(1).strip().lower()
            if not (status.startswith('unanswered') or status.startswith('waiting')):
                continue  # explicitly resolved/done/answered — skip
        # No status field, or status is unanswered/waiting → notify
        questions.append({"id": title[:40], "title": title})

    # Also recognize the free-form bullet format the proactive-loop and skills
    # actually append in: `- **[label, timestamp]** ...`. The `## `-section walk
    # above misses these entirely (real pending-questions.md carries 0 `## `
    # headings, only bullets), which silently zeroed the count and suppressed
    # every notification. Bullets follow the same "no Status field ⇒ unanswered"
    # convention as prose sections (resolved items are deleted, not marked).
    seen = {q["title"] for q in questions}
    for m in re.finditer(r'^\s*-\s+\*\*\[(.+?)\]', content, flags=re.MULTILINE):
        title = m.group(1).strip()
        if title and title not in seen:
            seen.add(title)
            questions.append({"id": title[:40], "title": title})
    return questions


def should_notify():
    """Only notify once per hour to avoid spam."""
    if not LAST_NOTIFY_FILE.exists():
        return True
    last = LAST_NOTIFY_FILE.stat().st_mtime
    return (time.time() - last) > 3600  # 1 hour


def notify_macos(count, titles):
    msg = f"{count} pending question{'s' if count > 1 else ''}: {', '.join(titles[:3])}"
    subprocess.run([
        "osascript", "-e",
        f'display notification "{msg}" with title "Sutando"'
    ], capture_output=True)


def notify_voice(questions):
    """Write to results/ so voice agent can speak it."""
    ts = int(time.time() * 1000)
    path = RESULTS_DIR / f"question-{ts}.txt"
    titles = [q["title"] for q in questions]
    path.write_text(
        f"You have {len(questions)} pending question{'s' if len(questions) > 1 else ''} waiting for your answer: "
        + "; ".join(titles)
        + ". Check the Questions tab in the web UI."
    )


def notify_discord_dm(questions):
    """Write a proactive-*.txt file so discord-bridge DMs the owner.
    Owner asked (2026-04-09, while traveling) to receive pending-question
    pings as DMs instead of just macOS notifications."""
    ts = int(time.time())
    path = RESULTS_DIR / f"proactive-pending-q-{ts}.txt"
    lines = [
        f"⚠️ {len(questions)} pending question{'s' if len(questions) > 1 else ''} waiting:",
        "",
    ]
    for q in questions[:5]:
        lines.append(f"• {q['title']}")
    if len(questions) > 5:
        lines.append(f"…and {len(questions) - 5} more")
    lines.append("")
    lines.append("Reply here or edit pending-questions.md on the Mini to resolve.")
    path.write_text("\n".join(lines))


def main():
    force = "--force" in sys.argv
    questions = get_waiting_questions()
    if not questions:
        return

    if not force and presenter_mode_active():
        print(f"(presenter-mode) {len(questions)} pending questions — suppressed")
        return

    if not force and not should_notify():
        print(f"(cooldown) {len(questions)} pending questions — skipping notification")
        return

    count = len(questions)
    titles = [q["title"] for q in questions]

    # macOS notification
    notify_macos(count, titles)

    # Voice result — only when voice is actually connected. When offline, the
    # discord-bridge dm-fallback would deliver question-*.txt as a duplicate
    # of notify_discord_dm below. Skipping cuts the spam in half.
    if voice_client_connected():
        notify_voice(questions)

    # Discord DM to owner (via discord-bridge poll_proactive)
    notify_discord_dm(questions)

    # Update last notify time
    LAST_NOTIFY_FILE.write_text(str(int(time.time())))

    print(f"Notified: {count} pending questions")


if __name__ == "__main__":
    main()
