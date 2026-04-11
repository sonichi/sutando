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

WORKSPACE = Path(__file__).parent.parent
PQ_FILE = WORKSPACE / "pending-questions.md"
RESULTS_DIR = WORKSPACE / "results"
LAST_NOTIFY_FILE = WORKSPACE / ".last-pq-notify"


def get_waiting_questions():
    """Parse pending-questions.md — matches both legacy `## Q1 — Title` and
    the current `## Title` / `- **Status:** unanswered` format."""
    if not PQ_FILE.exists():
        return []
    content = PQ_FILE.read_text()
    questions = []
    # Walk each ## section; a section is waiting if its body contains
    # `Status: unanswered` or `Status: Waiting`.
    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    for sec in sections[1:]:  # skip pre-header
        title_line, _, body = sec.partition('\n')
        title = title_line.strip()
        if not title:
            continue
        status_m = re.search(r'\*\*Status:\*\*\s*(.+)', body)
        if not status_m:
            continue
        status = status_m.group(1).strip().lower()
        if status.startswith('unanswered') or status.startswith('waiting'):
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

    if not force and not should_notify():
        print(f"(cooldown) {len(questions)} pending questions — skipping notification")
        return

    count = len(questions)
    titles = [q["title"] for q in questions]

    # macOS notification
    notify_macos(count, titles)

    # Voice result (if voice is connected, agent will speak it)
    notify_voice(questions)

    # Discord DM to owner (via discord-bridge poll_proactive)
    notify_discord_dm(questions)

    # Update last notify time
    LAST_NOTIFY_FILE.write_text(str(int(time.time())))

    print(f"Notified: {count} pending questions")


if __name__ == "__main__":
    main()
