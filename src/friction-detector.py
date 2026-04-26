#!/usr/bin/env python3
"""Proactive friction detector for Sutando.

Scans for things the user might not notice are building up:
- Stale pending questions (unanswered >24h)
- Old unprocessed tasks
- Overdue reminders
- GitHub issues/PRs needing attention
- Recurring meetings with no recent notes

Output: results/friction-{date}.txt
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
RESULTS_DIR = WORKSPACE / "results"


def check_pending_questions():
    """Find questions unanswered for >24h.

    pending-questions.md uses sections like:
        ## Question Title
        - **Asked:** 2026-04-06
        - **Question:** ...
        - **Status:** unanswered

    A previous version of this parser looked for lines starting with `- [`
    which never matched the actual format, so it always returned an empty
    list and friction-detector silently missed every unanswered question.
    """
    pq = WORKSPACE / "pending-questions.md"
    if not pq.exists():
        return []
    content = pq.read_text()
    if "(No pending questions)" in content or not content.strip():
        return []

    issues = []
    today = datetime.now().date()

    # Walk sections — each starts with `## Title`. Inside the section, look
    # for `Status: unanswered` and an `Asked:` date.
    current_title = None
    current_asked = None
    current_status = None

    def flush():
        if current_title and current_status == "unanswered":
            age_str = ""
            if current_asked:
                try:
                    asked_date = datetime.fromisoformat(current_asked).date()
                    age_days = (today - asked_date).days
                    age_str = f" ({age_days}d old)"
                except ValueError:
                    pass
            issues.append(f"Pending question unanswered{age_str}: {current_title[:80]}")

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current_title = stripped[3:].strip()
            current_asked = None
            current_status = None
            continue
        # Match `- **Asked:** 2026-04-06`
        if "**Asked:**" in stripped:
            try:
                current_asked = stripped.split("**Asked:**", 1)[1].strip().split()[0]
            except IndexError:
                pass
        # Match `- **Status:** unanswered`
        if "**Status:**" in stripped:
            try:
                current_status = stripped.split("**Status:**", 1)[1].strip().lower().split()[0]
            except IndexError:
                pass
    flush()  # don't forget the last section

    return issues


def check_stale_tasks():
    """Find task files older than 1 hour (should be processed within minutes)."""
    issues = []
    tasks_dir = WORKSPACE / "tasks"
    if not tasks_dir.exists():
        return []
    now = datetime.now().timestamp()
    for f in tasks_dir.glob("task-*.txt"):
        age_hours = (now - f.stat().st_mtime) / 3600
        if age_hours > 1:
            issues.append(f"Stale task unprocessed for {age_hours:.0f}h: {f.name}")
    return issues


def check_github_issues():
    """Find open issues/PRs that haven't been updated in >7 days."""
    issues = []
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--json", "number,title,updatedAt"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            items = json.loads(result.stdout)
            now = datetime.utcnow()
            for item in items:
                updated = datetime.fromisoformat(item["updatedAt"].replace("Z", "+00:00")).replace(tzinfo=None)
                age_days = (now - updated).days
                if age_days > 7:
                    issues.append(f"GitHub issue #{item['number']} stale ({age_days}d): {item['title'][:60]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return issues


def check_overdue_reminders():
    """Check macOS Reminders for overdue items."""
    issues = []
    try:
        script = WORKSPACE.parent.parent / ".claude" / "skills" / "macos-tools" / "scripts" / "reminders.py"
        if not script.exists():
            return []
        # Use sys.executable: friction-detector runs via cron (launchd-managed);
        # bare `python3` can resolve to a different interpreter on minimal PATH.
        # See feedback_subprocess_sys_executable.md.
        result = subprocess.run(
            [sys.executable, str(script), "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "overdue" in line.lower() or "past due" in line.lower():
                    issues.append(f"Overdue reminder: {line.strip()[:80]}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return issues


def check_stale_results():
    """Find undelivered results (no corresponding task completion)."""
    # Not critical — skip for now
    return []


def check_notes_without_follow_up():
    """Find notes tagged 'action' or 'todo' that are >7 days old."""
    issues = []
    notes_dir = WORKSPACE / "notes"
    if not notes_dir.exists():
        return []
    now = datetime.now().timestamp()
    for f in notes_dir.glob("*.md"):
        content = f.read_text()
        # Only match explicit TODO markers in content body (not tags)
        lines = content.split("\n")
        body_start = False
        has_todo = False
        for line in lines:
            if body_start and line.strip().startswith("---"):
                continue
            if line.strip() == "---":
                body_start = not body_start
                continue
            if body_start or not line.startswith("---"):
                low = line.lower()
                # Match markers only at the start of the line (after optional
                # leading whitespace and a list bullet). The previous "any
                # marker anywhere in line" rule false-positived on
                # documentation prose like "- Action: Get Contents of URL"
                # in the Apple Shortcuts research note (the word "Action:"
                # is shortcut-terminology, not a TODO directive).
                stripped = low.lstrip(" \t-*")
                # "action:" was dropped: too noisy. It's standard prose-label
                # vocabulary (e.g. "Action: Get Contents of URL" in shortcut
                # docs, "Action items:" as a section header, etc.). The other
                # three are unambiguously directive.
                if "- [ ]" in low or any(stripped.startswith(m) for m in ("todo:", "follow-up:", "followup:")):
                    has_todo = True
                    break
                # Also match tags line with explicit 'todo' tag
                if "tags:" in low and "todo" in low:
                    has_todo = True
                    break
        if has_todo:
            age_days = (now - f.stat().st_mtime) / 86400
            if age_days > 7:
                title = f.stem.replace("-", " ").title()
                issues.append(f"Note with action items ({age_days:.0f}d old): {title}")
    return issues


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    output_path = RESULTS_DIR / f"friction-{today}.txt"

    # Don't regenerate if already done today
    if output_path.exists():
        print(f"Friction check already done today: {output_path}")
        print(output_path.read_text())
        return

    all_issues = []
    all_issues.extend(check_pending_questions())
    all_issues.extend(check_stale_tasks())
    all_issues.extend(check_github_issues())
    all_issues.extend(check_overdue_reminders())
    all_issues.extend(check_notes_without_follow_up())

    if not all_issues:
        summary = "No friction detected today. Everything is clean."
    else:
        summary = f"Found {len(all_issues)} item(s) that may need attention:\n"
        for i, issue in enumerate(all_issues, 1):
            summary += f"  {i}. {issue}\n"

    output_path.write_text(summary)
    print(f"Friction check → {output_path}")
    print(summary)


if __name__ == "__main__":
    main()
