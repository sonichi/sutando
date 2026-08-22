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
import re
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import claude_home_path, personal_path, shared_personal_path  # noqa: E402
from pending_questions_md import DIVIDER_OR_DONE_RE, active_region  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
RESULTS_DIR = WORKSPACE / "results"


# Prefix for "this probe did not run". A friction report that cannot distinguish
# "checked, found nothing" from "could not check" will happily tell the owner
# "Everything is clean" over probes that never executed — the same class of bug
# as the morning briefing's all-clear (#2528). Marked items are real report
# lines, so `all_issues` is non-empty and the all-clear is withheld.
UNCHECKED = "COULD NOT CHECK: "

# pending-questions.md is enumerated in full only while the list is short.
# Past this it collapses to a count + the oldest few; see check_pending_questions.
_PQ_ENUMERATE_MAX = 5
_PQ_OLDEST_SHOWN = 3


def check_pending_questions():
    """Find questions unanswered for >24h.

    pending-questions.md is free-form: sections start with ## Title and
    may or may not carry **Status:** markers. Per the #1265 / #1404
    convention: a section is open unless it is explicitly resolved.
    Only text ABOVE a `# Resolved` divider is scanned; the archive below it
    is ignored (this calls active_region(), which returns text up to the divider).
    """
    pq = Path(personal_path("pending-questions.md", WORKSPACE))
    if not pq.exists():
        return []
    content = pq.read_text()
    if "(No pending questions)" in content or not content.strip():
        return []

    # Discard resolved section (below a `# Resolved` / `# Done` divider).
    content = active_region(content, DIVIDER_OR_DONE_RE)

    _RESOLVED_STATUS = re.compile(
        r'\*\*Status:\*\*\s*(?:resolved|answered|done|complete)',
        re.IGNORECASE,
    )

    issues = []
    found: list = []
    today = datetime.now().date()
    current_title: Optional[str] = None
    current_asked: Optional[str] = None
    current_body_lines: list = []

    def flush() -> None:
        if not current_title:
            return
        body = "\n".join(current_body_lines)
        # Skip explicitly resolved sections.
        if _RESOLVED_STATUS.search(body):
            return
        age_str = ""
        age_days_num = -1  # unknown age sorts last, never ahead of a dated item
        if current_asked:
            try:
                asked_date = datetime.fromisoformat(current_asked).date()
                age_days = (today - asked_date).days
                if age_days < 1:
                    return  # not stale yet
                age_str = f" ({age_days}d old)"
                age_days_num = age_days
            except ValueError:
                pass
        found.append((age_days_num, f"Pending question unanswered{age_str}: {current_title[:80]}"))

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current_title = stripped[3:].strip()
            current_asked = None
            current_body_lines = []
            continue
        if current_title is not None:
            current_body_lines.append(stripped)
            if "**Asked:**" in stripped:
                try:
                    current_asked = stripped.split("**Asked:**", 1)[1].strip().split()[0]
                except IndexError:
                    pass
    flush()

    # check-pending-questions.py owns this list and rate-limits it; enumerating
    # it here buries the signal no other probe produces.
    if len(found) > _PQ_ENUMERATE_MAX:
        # `**Asked:**` is inconsistent, so sorting an undated file is a no-op:
        # only claim "oldest" when something is actually dated.
        dated = [p for p in found if p[0] >= 0]
        if dated:
            dated.sort(key=lambda p: p[0], reverse=True)
            label, sample = "oldest", dated[:_PQ_OLDEST_SHOWN]
        else:
            label, sample = "including", found[:_PQ_OLDEST_SHOWN]
        issues.append(f"{len(found)} pending questions unanswered "
                      f"(check-pending-questions.py owns the full list); {label}:")
        issues.extend(f"  {t}" for _, t in sample)
    else:
        issues.extend(t for _, t in found)

    return issues


def check_stale_tasks():
    """Find task files older than 1 hour with no result anywhere.

    A top-level task file can outlive a completed result when a consumer fails
    to archive the pair.  Treating the task file alone as pending produced an
    853-item false alarm (and repeated owner DMs) even though every task had a
    matching result.  Mirror the queue health check's completion namespaces:
    live results, bridge archives, and startup retention archives.
    """
    issues = []
    tasks_dir = WORKSPACE / "tasks"
    if not tasks_dir.exists():
        return []

    completed_names = set()

    def record_result(path: Path) -> None:
        if not path.is_file():
            return
        completed_names.add(path.name)
        renamed = re.match(r"^(.+)-[0-9]+\.txt$", path.name)
        if renamed:
            completed_names.add(f"{renamed.group(1)}.txt")

    for path in RESULTS_DIR.glob("task-*.txt"):
        record_result(path)
    for path in (RESULTS_DIR / "archive").glob("*.txt"):
        record_result(path)
    for path in (RESULTS_DIR / "archive").glob("*/*.txt"):
        record_result(path)
    for retention_dir in RESULTS_DIR.glob("archive-*"):
        if retention_dir.is_dir():
            for path in retention_dir.glob("*.txt"):
                record_result(path)

    now = datetime.now().timestamp()
    for f in tasks_dir.glob("task-*.txt"):
        if f.name in completed_names:
            continue
        age_hours = (now - f.stat().st_mtime) / 3600
        if age_hours > 1:
            issues.append(f"Stale task unprocessed for {age_hours:.0f}h: {f.name}")
    return issues


# `gh issue list` defaults to 30 rows newest-first, so a staleness probe must
# page past it. Query and report bounds are separate on purpose.
_GH_QUERY_LIMIT = 500
_GH_REPORT_CAP = 10


def check_github_issues():
    """Find open issues not updated in >7 days. Issues only — open PRs are
    `pr_flag.py`'s domain and would otherwise be reported twice."""
    issues = []
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--limit", str(_GH_QUERY_LIMIT),
             "--json", "number,title,updatedAt"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            # A failed probe is not an absence of stale issues. Saying nothing
            # here lets `all_issues == []` render as "Everything is clean" over
            # a question that was never answered.
            return [UNCHECKED + "GitHub issues (gh exited "
                    f"{result.returncode})"]
        items = json.loads(result.stdout)
        if len(items) >= _GH_QUERY_LIMIT:
            # Hit the cap we passed: the read is truncated and the oldest issues
            # are the ones most likely missing. Say so rather than under-report.
            return [UNCHECKED + f"GitHub issues (returned {len(items)} == the "
                    f"{_GH_QUERY_LIMIT} cap, so the list is truncated)"]
        now = datetime.now(timezone.utc)
        stale = []
        for item in items:
            updated = datetime.fromisoformat(item["updatedAt"].replace("Z", "+00:00"))
            age_days = (now - updated).days
            if age_days > 7:
                stale.append((age_days, item))
        stale.sort(key=lambda p: p[0], reverse=True)
        for age_days, item in stale[:_GH_REPORT_CAP]:
            issues.append(f"GitHub issue #{item['number']} stale ({age_days}d): {item['title'][:60]}")
        if len(stale) > _GH_REPORT_CAP:
            issues.append(f"...and {len(stale) - _GH_REPORT_CAP} more stale issue(s) "
                          f"beyond the {_GH_REPORT_CAP} oldest shown")
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        return [UNCHECKED + f"GitHub issues ({type(e).__name__})"]
    return issues


def check_overdue_reminders():
    """Check macOS Reminders for overdue items."""
    issues = []
    try:
        script = claude_home_path("skills", "macos-tools", "scripts", "reminders.py")
        if not script.exists():
            # Absent probe, not an absent problem. This is also why the suite
            # fails on a clean-install runner where macos-tools is not present:
            # the early return skipped the exception handler entirely.
            return [UNCHECKED + "overdue reminders (reminders.py not installed)"]
        # Use sys.executable: friction-detector runs via cron (launchd-managed);
        # bare `python3` can resolve to a different interpreter on minimal PATH.
        # See feedback_subprocess_sys_executable.md.
        result = subprocess.run(
            [sys.executable, str(script), "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return [UNCHECKED + f"overdue reminders (reminders.py exited "
                    f"{result.returncode})"]
        for line in result.stdout.split("\n"):
            if "overdue" in line.lower() or "past due" in line.lower():
                issues.append(f"Overdue reminder: {line.strip()[:80]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return [UNCHECKED + f"overdue reminders ({type(e).__name__})"]
    return issues


def check_notes_without_follow_up():
    """Find notes tagged 'action' or 'todo' that are >7 days old."""
    issues = []
    notes_dir = Path(shared_personal_path("notes", WORKSPACE))
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
