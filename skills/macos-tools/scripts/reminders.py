#!/usr/bin/env python3
"""
Sutando reminders — read/write macOS Reminders via AppleScript.

Usage:
  python3 src/reminders.py list                              # all incomplete reminders
  python3 src/reminders.py list --all                        # include completed
  python3 src/reminders.py list --due-today                  # only today's + overdue
  python3 src/reminders.py add "Buy groceries"               # add to default list
  python3 src/reminders.py add "Call Bob" "2026-03-17"       # add with due date
  python3 src/reminders.py add "Fix bug" "" "Work"           # add to specific list
  python3 src/reminders.py complete "Buy groceries"          # mark as done
  python3 src/reminders.py lists                             # show all reminder lists
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

_app_launched = False

def _ensure_reminders_running():
    global _app_launched
    if not _app_launched:
        subprocess.run(["open", "-ga", "Reminders"], capture_output=True, timeout=5)
        time.sleep(1)
        _app_launched = True


def run_applescript(script: str) -> tuple[str, str]:
    _ensure_reminders_running()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip(), result.stderr.strip()


def list_reminder_lists() -> list[str]:
    out, err = run_applescript('tell application "Reminders" to get name of every list')
    if err:
        return []
    return [n.strip() for n in out.split(",") if n.strip()]


def list_reminders(include_completed: bool = False) -> list[dict]:
    completed_filter = "" if include_completed else "whose completed is false"
    script = f"""
tell application "Reminders"
    set output to ""
    repeat with aList in every list
        set listName to name of aList
        set rems to (reminders of aList {completed_filter})
        repeat with r in rems
            set rName to name of r
            set rDone to completed of r
            try
                set rDue to due date of r
                set rDueStr to (rDue as string)
            on error
                set rDueStr to ""
            end try
            try
                set rBody to body of r
            on error
                set rBody to ""
            end try
            set output to output & listName & "|||" & rName & "|||" & rDueStr & "|||" & (rDone as string) & "|||" & rBody & "\\n"
        end repeat
    end repeat
    return output
end tell
"""
    out, err = run_applescript(script)
    if err:
        return [{"error": err}]

    reminders = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        due = parts[2].strip()
        body = parts[4].strip() if len(parts) > 4 else ""
        reminders.append({
            "list": parts[0].strip(),
            "name": parts[1].strip(),
            "due": "" if due == "missing value" else due,
            "completed": parts[3].strip().lower() == "true",
            "body": "" if body == "missing value" else body,
        })
    return reminders


_APPLESCRIPT_DATE_FMT = "%A, %B %d, %Y at %I:%M:%S %p"


def _is_due_today_or_overdue(due_str: str) -> bool:
    """Return True if due_str (AppleScript date format) is today or in the past."""
    if not due_str:
        return False
    try:
        due_date = datetime.strptime(due_str, _APPLESCRIPT_DATE_FMT).date()
        return due_date <= datetime.now().date()
    except ValueError:
        return False


def add_reminder(name: str, due_date: str = "", list_name: str = "") -> str:
    target = f'list "{list_name}"' if list_name else "default list"
    due_clause = ""
    if due_date:
        due_clause = f', due date:date "{due_date}"'

    script = f"""
tell application "Reminders"
    tell {target}
        make new reminder with properties {{name:"{name}"{due_clause}}}
    end tell
end tell
"""
    out, err = run_applescript(script)
    if err:
        return f"Error: {err}"
    return f"Added: {name}" + (f" (due {due_date})" if due_date else "")


def complete_reminder(name: str) -> str:
    script = f"""
tell application "Reminders"
    repeat with aList in every list
        set rems to (reminders of aList whose name is "{name}" and completed is false)
        repeat with r in rems
            set completed of r to true
            return "Done: " & name
        end repeat
    end repeat
    return "Not found: " & "{name}"
end tell
"""
    out, err = run_applescript(script)
    if err:
        return f"Error: {err}"
    return out or f"Not found: {name}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 src/reminders.py [list|add|complete|lists]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "lists":
        for name in list_reminder_lists():
            print(f"  - {name}")

    elif cmd == "list":
        include_all = "--all" in sys.argv
        due_today = "--due-today" in sys.argv
        reminders = list_reminders(include_completed=include_all)
        if not reminders:
            print("No reminders.")
            return
        if reminders and "error" in reminders[0]:
            print(f"Error: {reminders[0]['error']}", file=sys.stderr)
            sys.exit(1)
        if due_today:
            reminders = [r for r in reminders if _is_due_today_or_overdue(r["due"])]
        if not reminders:
            print("No reminders.")
            return
        for r in reminders:
            done = " [DONE]" if r["completed"] else ""
            due = f" (due {r['due']})" if r["due"] else ""
            print(f"  [{r['list']}] {r['name']}{due}{done}")

    elif cmd == "add":
        if len(sys.argv) < 3:
            print("Usage: python3 src/reminders.py add 'name' ['due_date'] ['list']")
            sys.exit(1)
        name = sys.argv[2]
        due = sys.argv[3] if len(sys.argv) > 3 else ""
        lst = sys.argv[4] if len(sys.argv) > 4 else ""
        print(add_reminder(name, due, lst))

    elif cmd == "complete":
        if len(sys.argv) < 3:
            print("Usage: python3 src/reminders.py complete 'name'")
            sys.exit(1)
        print(complete_reminder(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
