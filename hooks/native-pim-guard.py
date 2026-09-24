#!/usr/bin/env python3
"""native-pim-guard — PreToolUse hook on Bash that denies commands driving the
native macOS Calendar, Reminders or Contacts apps without the owner's consent.

Driving those apps (``osascript``/JXA ``tell application "Calendar"``, ``open -a
Calendar``, …) raises a macOS Automation permission prompt on the owner's screen.
Calendar work goes through the Station connector; the local apps are only for an
owner who asked for them in this conversation.

Escape hatch: the command carries the env prefix ``SUTANDO_ALLOW_NATIVE_PIM=1``
(or the hook's own environment has it). Fail-OPEN on any error, like
gmail-write-guard.py: a crashing hook must never wedge the core.
"""
import json
import os
import re
import sys

APPS = r"(?:Calendar|iCal|Reminders|Contacts|Address\s?Book)"

# AppleScript/JXA targets: `tell application "Calendar"`, `app "Reminders"`,
# `Application("Contacts")`, `of application "Calendar"`, `using terms from …`.
SCRIPT_TARGET = re.compile(
    r"""(?:\bapp(?:lication)?\s*["']\s*%s\s*["']|\bApplication\s*\(\s*["']%s["']\s*\))""" % (APPS, APPS),
    re.IGNORECASE,
)
# `open -a Calendar`, `open -gja Reminders`, `open -b com.apple.iCal`,
# `open …/Contacts.app` — any launch of the four apps by name, bundle id or path.
OPEN_APP = re.compile(
    r"""\bopen\b[^;&|\n]*?(?:-[A-Za-z]*a[A-Za-z]*\s+["']?%s\b|-b\s+["']?com\.apple\.(?:iCal|reminders|AddressBook)\b|%s\.app\b)"""
    % (APPS, APPS),
    re.IGNORECASE,
)
SCRIPT_RUNNER = re.compile(r"\b(?:osascript|automator)\b")
ESCAPE_HATCH = re.compile(r"(?:^|[\s;&|(`])SUTANDO_ALLOW_NATIVE_PIM=1(?:\s|$)")

REASON = (
    "Blocked: this command drives the native macOS Calendar/Reminders/Contacts app, which raises a "
    "macOS permission prompt the owner did not ask for. Order: (1) the Station connector first: "
    "composio_find {\"apps\": [\"google calendar\"]} then composio_exec with toolkit \"googlecalendar\" "
    "(google contacts / google tasks for the other two); (2) if the app is not connected, the "
    "owner's own calendar tools when they are in your tool list (mcp__claude_ai_Google_Calendar__*); "
    "(3) otherwise ask the owner what to do. Never open Calendar.app, Reminders or Contacts on your "
    "own, and never re-prompt once the owner denied the permission. Only when the owner asked for the "
    "local app in this conversation, run it with the env prefix SUTANDO_ALLOW_NATIVE_PIM=1. "
    "[native-pim-guard]"
)


def targets_native_pim(command: str) -> bool:
    """True when the Bash command scripts or launches one of the four apps."""
    if OPEN_APP.search(command):
        return True
    return bool(SCRIPT_RUNNER.search(command) and SCRIPT_TARGET.search(command))


def owner_allowed(command: str) -> bool:
    return bool(ESCAPE_HATCH.search(command)) or \
        os.environ.get("SUTANDO_ALLOW_NATIVE_PIM", "").strip() == "1"


def main() -> None:
    data = json.loads(sys.stdin.read())
    if str(data.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = str((data.get("tool_input") or {}).get("command") or "")
    if targets_native_pim(command) and not owner_allowed(command):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[native-pim-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
