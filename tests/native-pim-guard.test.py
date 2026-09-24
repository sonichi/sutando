#!/usr/bin/env python3
"""PreToolUse native-pim-guard: Bash commands that script or launch the native
macOS Calendar/Reminders/Contacts apps are denied unless the owner consented
(hooks/native-pim-guard.py).

Behavioral: drives the real hook via stdin exactly the way Claude Code invokes
it, asserting on the emitted decision JSON + exit code.

Run:  python3 tests/native-pim-guard.test.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = str(REPO / "hooks" / "native-pim-guard.py")
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(payload, env_extra=None):
    stdin = json.dumps(payload) if not isinstance(payload, str) else payload
    env = dict(os.environ)
    env.pop("SUTANDO_ALLOW_NATIVE_PIM", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(PYBASE + [HOOK], input=stdin,
                          capture_output=True, text=True, timeout=20, env=env)


def bash(command, env_extra=None):
    return run({"tool_name": "Bash", "tool_input": {"command": command}}, env_extra)


def decision(r):
    try:
        return json.loads(r.stdout).get("hookSpecificOutput", {}).get("permissionDecision")
    except Exception:
        return None


# ── Every app form is denied: AppleScript, JXA, open -a/-ga/-gja, bundle id, .app path ──
DENIED = [
    """osascript -e 'tell application "Calendar" to get every calendar'""",
    """osascript -e 'tell app "iCal" to get name of every calendar'""",
    """osascript -e 'tell application "Reminders" to get name of every list'""",
    """osascript -e 'tell application "Contacts" to get name of every person'""",
    """osascript -e 'tell application "Address Book" to get every person'""",
    """osascript -l JavaScript -e 'Application("Calendar").calendars()'""",
    """osascript -l JavaScript -e "Application('Reminders').lists()" """,
    "osascript -e 'using terms from application \"Calendar\"' -e 'return 1'",
    "open -a Calendar",
    "open -ga Reminders",
    "open -gja Calendar && sleep 3",
    "open -a Contacts",
    "open -a 'Address Book'",
    "open -b com.apple.iCal",
    "open -b com.apple.reminders",
    "open -b com.apple.AddressBook",
    "open /System/Applications/Calendar.app",
    "open /System/Applications/Reminders.app",
    "OTHER=1 osascript -e 'tell application \"Calendar\" to launch'",
]
for cmd in DENIED:
    r = bash(cmd)
    check(f"deny: {cmd[:60]}", r.returncode == 0 and decision(r) == "deny", r.stdout[:120])

# Reason carries the order the model must follow and the never-re-prompt rule.
r = bash("open -a Calendar")
reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
check("reason names the Station connector first", "composio_find" in reason and "composio_exec" in reason)
check("reason names the owner's own MCP calendar tools second", "mcp__claude_ai_Google_Calendar__" in reason)
check("reason says to ask the owner third", "ask the owner" in reason)
check("reason forbids re-prompting after a denial", "never re-prompt" in reason)
check("reason names the escape hatch", "SUTANDO_ALLOW_NATIVE_PIM=1" in reason)

# ── Unrelated osascript / open commands pass through ──────────────────────────
ALLOWED = [
    """osascript -e 'display notification "Calendar synced" with title "Sutando"'""",
    """osascript -e 'tell application "System Events" to get name of every process'""",
    """osascript -e 'tell application "Finder" to get name of every window'""",
    "osascript -e 'do shell script \"defaults read /Library/Preferences/com.apple.timezone\"'",
    "open -a Safari https://example.com",
    "open -a 'Google Chrome'",
    "open ./notes/calendar-plan.md",
    "python3 skills/macos-tools/scripts/calendar-reader.py 1 text --owner-asked",
    "grep -rn Calendar docs/",
    "echo 'tell application \"Calendar\"'",
]
for cmd in ALLOWED:
    r = bash(cmd)
    check(f"allow: {cmd[:60]}",
          r.returncode == 0 and decision(r) is None and not r.stdout.strip(), r.stdout[:120])

# ── Non-Bash tools are untouched (safe under a broad matcher) ─────────────────
for t in ["Read", "mcp__sutando-station__composio_exec", "mcp__claude_ai_Google_Calendar__list_events"]:
    r = run({"tool_name": t, "tool_input": {"command": "open -a Calendar"}})
    check(f"allow non-bash: {t}", r.returncode == 0 and decision(r) is None and not r.stdout.strip())

# ── Escape hatch: the env prefix on the command, or the hook's own environment ──
for cmd in [
    "SUTANDO_ALLOW_NATIVE_PIM=1 osascript -e 'tell application \"Calendar\" to get every calendar'",
    "SUTANDO_ALLOW_NATIVE_PIM=1 open -ga Reminders",
    "cd /tmp && SUTANDO_ALLOW_NATIVE_PIM=1 python3 x.py; open -a Contacts",
]:
    r = bash(cmd)
    check(f"prefix lifts the guard: {cmd[:60]}", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])

r = bash("open -a Calendar", env_extra={"SUTANDO_ALLOW_NATIVE_PIM": "1"})
check("SUTANDO_ALLOW_NATIVE_PIM=1 in the hook env lifts the guard",
      r.returncode == 0 and not r.stdout.strip())

# A look-alike prefix does not count.
for cmd in ["SUTANDO_ALLOW_NATIVE_PIM=10 open -a Calendar",
            "SUTANDO_ALLOW_NATIVE_PIM=0 open -a Calendar",
            "echo SUTANDO_ALLOW_NATIVE_PIM=1; open -a Calendar"]:
    r = bash(cmd)
    check(f"look-alike prefix still denied: {cmd[:60]}", decision(r) == "deny", r.stdout[:120])

# ── Bad input: fail-OPEN (exit 0, no deny) — never wedge the core ─────────────
r = run("this is not json")
check("malformed stdin fails open", r.returncode == 0 and decision(r) is None)
r = run({"tool_name": "Bash", "tool_input": "not an object"})
check("non-object tool_input fails open", r.returncode == 0 and decision(r) is None)
r = run({"tool_name": "Bash"})
check("missing tool_input allows", r.returncode == 0 and decision(r) is None)

if failures:
    print(f"\nFAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("\nPASS — native-pim-guard tests")
