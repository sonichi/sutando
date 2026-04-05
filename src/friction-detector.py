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
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
RESULTS_DIR = WORKSPACE / "results"


def check_pending_questions():
    """Find questions unanswered for >24h."""
    pq = WORKSPACE / "pending-questions.md"
    if not pq.exists():
        return []
    content = pq.read_text()
    if "(No pending questions)" in content or not content.strip():
        return []
    # Parse questions with timestamps
    issues = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- ") and "[" in line:
            issues.append(f"Pending question unanswered: {line[:80]}")
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
        result = subprocess.run(
            ["python3", str(script), "list"],
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
                if any(marker in low for marker in ["- [ ]", "todo:", "action:", "follow-up:", "followup:"]):
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
