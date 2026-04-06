#!/usr/bin/env python3
"""Check pending questions and notify if unanswered.

Runs on cron — independent of the proactive loop.
Sends notifications via macOS + Discord DM if questions are waiting.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
PQ_FILE = WORKSPACE / "pending-questions.md"
RESULTS_DIR = WORKSPACE / "results"
LAST_NOTIFY_FILE = WORKSPACE / ".last-pq-notify"


def get_waiting_questions():
    if not PQ_FILE.exists():
        return []
    content = PQ_FILE.read_text()
    questions = []
    for m in re.finditer(r'## (Q\d+) — (.+?)\n', content):
        # Check if this question's status is Waiting
        qid = m.group(1)
        title = m.group(2)
        if f"**Status:** Waiting" in content[m.start():m.start() + 500]:
            questions.append({"id": qid, "title": title})
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


def main():
    questions = get_waiting_questions()
    if not questions:
        return

    if not should_notify():
        return

    count = len(questions)
    titles = [q["title"] for q in questions]

    # macOS notification
    notify_macos(count, titles)

    # Voice result (if voice is connected, agent will speak it)
    notify_voice(questions)

    # Update last notify time
    LAST_NOTIFY_FILE.write_text(str(int(time.time())))

    print(f"Notified: {count} pending questions")


if __name__ == "__main__":
    main()
