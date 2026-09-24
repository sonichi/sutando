#!/usr/bin/env python3
"""native-pim-guard — PreToolUse hook on Bash that denies commands driving the
native macOS Calendar, Reminders or Contacts apps without the owner's consent.

Driving those apps (``osascript``/JXA ``tell application "Calendar"``, ``open -a
Calendar``, a script file or ``shortcuts run`` that can do the same) raises a
macOS Automation permission prompt on the owner's screen. Calendar work goes
through the Station connector; the local apps are only for an owner who asked.

Consent, any of: the command's env prefix ``SUTANDO_ALLOW_NATIVE_PIM=1`` (at
command position), that variable in the hook's own environment, or the persisted
host opt-in ``<workspace>/state/native-pim-consent`` written by the owner with
``native_pim_consent.py grant`` — a command this hook denies to the agent, so the
owner runs it in their own terminal. The consent counts only on the owner's own
task: when ``state/bindings/active-execution.json`` names the running task and
its file resolves to a non-owner tier, the hatch is ignored. Without a binding
the consent is self-attested (the model wrote the string), so this hook guards
against acting on the agent's own initiative and is not an authorisation
boundary. Fail-OPEN on any error, like gmail-write-guard.py.
"""
import json
import os
import re
import sys
from pathlib import Path

APPS = r"(?:Calendar|iCal|Reminders|Contacts|Address\s?Book)"
BUNDLES = r"com\.apple\.(?:iCal|reminders|AddressBook)"

# AppleScript/JXA targets by name or bundle id: `tell application "Calendar"`,
# `application id "com.apple.iCal"`, `Application("Contacts")`, `using terms from …`.
SCRIPT_TARGET = re.compile(
    r"""(?:\bapp(?:lication)?\s*["']\s*%s\s*["']|\bapplication\s+id\s*["']%s["']"""
    r"""|\bApplication\s*\(\s*["'](?:%s|%s)["']\s*\))""" % (APPS, BUNDLES, APPS, BUNDLES),
    re.IGNORECASE,
)
# `open -a Calendar`, `open -gja Reminders`, `open -b com.apple.iCal`,
# `open …/Contacts.app`, `open x-apple-reminderkit://…` — any launch of the apps.
OPEN_APP = re.compile(
    r"""\bopen\b[^;&|\n]*?(?:-[A-Za-z]*a[A-Za-z]*\s+["']?%s\b|-b\s+["']?%s\b|%s\.app\b"""
    r"""|["']?x-apple-reminderkit:)""" % (APPS, BUNDLES, APPS),
    re.IGNORECASE,
)
SCRIPT_RUNNER = re.compile(r"\b(?:osascript|automator)\b")
SCRIPT_FILE = re.compile(r"""\bosascript\b[^;&|\n]*?\s(["']?)([^\s"';&|]+\.(?:scpt|scptd|applescript|js))\1""",
                         re.IGNORECASE)
SHORTCUTS_RUN = re.compile(r"\bshortcuts\s+run\b")
GRANT_COMMAND = re.compile(r"native_pim_consent(?:\.py)?\s+grant\b")
# The token must sit at command position (start, after ; & | ( or an env-prefix
# chain), so `echo SUTANDO_ALLOW_NATIVE_PIM=1; open -a Calendar` does not count.
ESCAPE_HATCH = re.compile(
    r"(?:^|[;&|(`\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*SUTANDO_ALLOW_NATIVE_PIM=1(?:\s|$)"
)
SCRIPT_FILE_READ_LIMIT = 65536

ORDER = (
    "Order: (1) the Station connector first: composio_find {\"apps\": [\"google calendar\"]} then "
    "composio_exec with toolkit \"googlecalendar\" (google contacts / google tasks for the other "
    "two); (2) if the app is not connected, the owner's own calendar tools when they are in your "
    "tool list (mcp__claude_ai_Google_Calendar__*); (3) otherwise ask the owner what to do. Never "
    "open Calendar.app, Reminders or Contacts on your own, and never re-prompt once the owner "
    "denied the permission."
)
REASON = (
    "Blocked: this command drives the native macOS Calendar/Reminders/Contacts app, which raises a "
    "macOS permission prompt the owner did not ask for. " + ORDER + " Only when the owner asked for "
    "the local app in this conversation, run it with the env prefix SUTANDO_ALLOW_NATIVE_PIM=1; the "
    "owner can allow the local apps for this host once with `python3 "
    "skills/macos-tools/scripts/native_pim_consent.py grant` in their own terminal. "
    "[native-pim-guard]"
)
REASON_NOT_OWNER_TASK = (
    "Blocked: SUTANDO_ALLOW_NATIVE_PIM=1 only counts on the owner's own task, and the task bound to "
    "this session is not owner-tier. The native macOS Calendar/Reminders/Contacts apps raise a "
    "permission prompt on the owner's screen. " + ORDER + " [native-pim-guard]"
)
REASON_GRANT = (
    "Blocked: `native_pim_consent.py grant` is the owner's own consent and is run by the owner in "
    "their own terminal, never by the agent. Tell the owner the command if they want the local "
    "Calendar/Reminders/Contacts apps used on this host. [native-pim-guard]"
)


def _script_file_targets(command: str) -> bool:
    """An osascript script file: the name or its first 64 KB names one of the apps."""
    for _q, path in SCRIPT_FILE.findall(command):
        if re.search(r"(?:%s|%s)" % (APPS, BUNDLES), os.path.basename(path), re.IGNORECASE):
            return True
        try:
            with open(os.path.expanduser(path), "rb") as f:
                head = f.read(SCRIPT_FILE_READ_LIMIT).decode("latin-1")
        except OSError:
            continue
        if SCRIPT_TARGET.search(head):
            return True
    return False


def targets_native_pim(command: str) -> bool:
    """True when the Bash command scripts or launches one of the apps."""
    if OPEN_APP.search(command) or SHORTCUTS_RUN.search(command):
        return True
    if SCRIPT_RUNNER.search(command) and SCRIPT_TARGET.search(command):
        return True
    return _script_file_targets(command)


def _workspace() -> Path:
    """The repo resolver when importable (same answer as native_pim_consent.py), else the config-dir walk."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        from workspace_default import resolve_workspace
        return Path(resolve_workspace())
    except Exception:
        pass
    p = os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    while True:
        if os.path.basename(p) == ".claude-sutando":
            return Path(os.path.dirname(p))
        parent = os.path.dirname(p)
        if parent == p:
            return Path(os.path.expanduser("~/sutando-workspace"))
        p = parent


def consent_marker_present(ws: Path) -> bool:
    return (ws / "state" / "native-pim-consent").exists()


def bound_task_tier(ws: Path):
    """Tier of the task bound to this session, or None when nothing is bound or readable."""
    try:
        with open(ws / "state" / "bindings" / "active-execution.json") as f:
            task_id = str(json.load(f).get("task_id") or "")
    except (OSError, ValueError):
        return None
    if not task_id or "/" in task_id or task_id.startswith("."):
        return None
    task_file = ws / "tasks" / f"{task_id}.txt"
    if not task_file.exists():
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        from policy.egress.result import resolve_access_tier
    except Exception:
        return None
    return resolve_access_tier(task_file)


def consent_given(command: str, ws: Path) -> bool:
    return bool(ESCAPE_HATCH.search(command)) or \
        os.environ.get("SUTANDO_ALLOW_NATIVE_PIM", "").strip() == "1" or \
        consent_marker_present(ws)


def decide(command: str, ws: Path):
    """The deny reason for this command, or None to allow."""
    if GRANT_COMMAND.search(command):
        return REASON_GRANT
    if not targets_native_pim(command):
        return None
    if not consent_given(command, ws):
        return REASON
    tier = bound_task_tier(ws)
    if tier is not None and tier != "owner":
        return REASON_NOT_OWNER_TASK
    return None


def main() -> None:
    data = json.loads(sys.stdin.read())
    if str(data.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = str((data.get("tool_input") or {}).get("command") or "")
    reason = decide(command, _workspace())
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
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
