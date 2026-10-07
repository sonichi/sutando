#!/usr/bin/env python3
"""native-pim-guard — PreToolUse hook on Bash (and the file tools) that denies
commands driving the native macOS Calendar, Reminders or Contacts apps without
the owner's consent, and any write to the consent record.

Driving those apps (``osascript``/JXA ``tell application "Calendar"``, ``open -a
Calendar``, a script file or ``shortcuts run`` that can do the same) raises a
macOS Automation permission prompt on the owner's screen. Calendar work goes
through the Station connector; the local apps are only for an owner who asked.
Only the command at command position counts (start, or after ``;`` ``&&`` ``|``
``(`` ``$(`` and the like): ``grep "open -a Calendar" src/`` is a read and passes;
a wrapper such as ``bash -c``, ``xargs`` or ``sudo`` is scanned whole.

Consent, any of: the command's env prefix ``SUTANDO_ALLOW_NATIVE_PIM=1`` (at
command position), that variable in the hook's own environment, or the persisted
host opt-in ``<workspace>/state/native-pim-consent`` written by the owner with
``native_pim_consent.py grant``. The agent never writes that consent: ``grant``,
any command that names ``state/native-pim-consent`` or a ``*-automation-denied``
marker outside a read-only command, and any Python that imports
``native_pim_consent`` are denied, and so is a Write / Edit / MultiEdit /
NotebookEdit whose path resolves to one of those markers, so the owner runs
``grant`` in their own terminal. The consent counts only on the owner's own task: when
``state/bindings/active-execution.json`` names the running task and its file
resolves to a non-owner tier, the hatch is ignored. Without a binding the consent
is self-attested (the model wrote the string), so this hook guards against acting
on the agent's own initiative and is not an authorisation boundary. The policy
itself (marker names, host opt-in, bound tier) is ``native_pim_consent.py``;
this hook only parses the command. Fail-OPEN on any error, like
gmail-write-guard.py.
"""
import json
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "src", _REPO / "skills" / "macos-tools" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import native_pim_consent as consent  # noqa: E402  (the one consent policy)

APPS = r"(?:Calendar|iCal|Reminders|Contacts|Address\s?Book)"
BUNDLES = r"com\.apple\.(?:iCal|reminders|AddressBook)"

# AppleScript/JXA targets by name or bundle id (`tell application "Calendar"`, `application id
# "com.apple.iCal"`, `Application("Contacts")`); a quote may carry a shell escape (`sh -c "…\"Calendar\"…"`).
Q = r"""\\?["']"""
SCRIPT_TARGET = re.compile(
    r"""(?:\bapp(?:lication)?\s*%s\s*%s\s*%s|\bapplication\s+id\s*%s%s%s"""
    r"""|\bApplication\s*\(\s*%s(?:%s|%s)%s\s*\))""" % (Q, APPS, Q, Q, BUNDLES, Q, Q, APPS, BUNDLES, Q),
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
CONSENT_MODULE = re.compile(r"\bnative_pim_consent\b")
# The CLI reads the state (`status`) or narrows it (`revoke`): the only module uses allowed.
CONSENT_READ_CLI = re.compile(
    r"^(?:\S*python[0-9.]*\s+)?\S*native_pim_consent\.py(?:\s+--workspace\s+\S+)?"
    r"\s+(?:status|revoke)(?:\s+--workspace\s+\S+)?\s*$")
# The marker files by name; the TS/JS/doc sources that mention them are not the markers.
MARKER_FILE = re.compile(
    r"\b(?:%s|(?:%s)%s)\b(?!\.(?:ts|js|mjs|md|py))" % (
        re.escape(consent.CONSENT_MARKER),
        "|".join(a.lower() for a in consent.APPS),
        re.escape(consent.DENIAL_MARKER_SUFFIX)),
    re.IGNORECASE,
)
# The token must sit at command position (start, after ; & | ( or an env-prefix
# chain), so `echo SUTANDO_ALLOW_NATIVE_PIM=1; open -a Calendar` does not count.
ESCAPE_HATCH = re.compile(
    r"(?:^|[;&|(`\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*SUTANDO_ALLOW_NATIVE_PIM=1(?:\s|$)"
)
SCRIPT_FILE_READ_LIMIT = 65536

# Not `(` alone: JXA `Application("Calendar")` must stay in one piece; `$(` and
# backticks do split, and a leading `(`/`{` is stripped from the segment.
SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|&\n`]|\$\()")
ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*")
# Commands that only read: an app name or a marker path inside them is text, not an action.
READ_ONLY = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "git", "cat", "head", "tail", "less", "more",
    "wc", "ls", "stat", "file", "diff", "echo", "printf", "test", "[", "which", "type",
})
# Wrappers hand their arguments to another command: scan the whole segment.
WRAPPERS = frozenset({
    "sudo", "env", "command", "nohup", "time", "nice", "xargs", "caffeinate", "bash", "sh",
    "zsh", "eval", "exec", "builtin", "script", "timeout", "gtimeout", "watch",
})

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
REASON_MARKER = (
    "Blocked: state/native-pim-consent and the *-automation-denied markers are the owner's consent "
    "record, and native_pim_consent is the policy behind it; the agent never writes, deletes or "
    "drives them. Read the state with `python3 skills/macos-tools/scripts/native_pim_consent.py "
    "status`; the owner grants with `… grant` in their own terminal, never the agent. "
    "[native-pim-guard]"
)
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def segments(command: str):
    """(command word, segment text) per shell segment; env-prefix assignments skipped."""
    out = []
    for raw in SEGMENT_SPLIT.split(command):
        seg = ENV_PREFIX.sub("", raw.strip().lstrip("({ \t"), count=1)
        if not seg:
            continue
        word = os.path.basename(seg.split(None, 1)[0].strip("\"'"))
        out.append((word.lower(), seg))
    return out


def _script_file_targets(segment: str) -> bool:
    """An osascript script file: the name or its first 64 KB names one of the apps."""
    for _q, path in SCRIPT_FILE.findall(segment):
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


def _segment_targets(word: str, seg: str, rest: str) -> bool:
    if word in READ_ONLY:
        return False
    if word == "open":
        return bool(OPEN_APP.search(seg))
    if word == "shortcuts":
        return bool(SHORTCUTS_RUN.search(seg))
    # A runner's script may continue past a `;` or a heredoc line; a wrapper's
    # argument is itself a command: both are scanned from here to the end.
    if word in ("osascript", "automator"):
        return bool(SCRIPT_TARGET.search(rest)) or _script_file_targets(rest)
    if word in WRAPPERS:
        if OPEN_APP.search(rest) or SHORTCUTS_RUN.search(rest):
            return True
        if SCRIPT_RUNNER.search(rest) and SCRIPT_TARGET.search(rest):
            return True
        return _script_file_targets(rest)
    return False


def targets_native_pim(command: str) -> bool:
    """True when a command at command position scripts or launches one of the apps."""
    segs = segments(command)
    for i, (word, seg) in enumerate(segs):
        rest = "\n".join(s for _w, s in segs[i:])
        if _segment_targets(word, seg, rest):
            return True
    return False


def _redirects_to_marker(seg: str) -> bool:
    return any(MARKER_FILE.search(t) for t in re.findall(r">{1,2}\s*[\"']?([^\s\"']+)", seg))


def touches_consent(command: str):
    """REASON_GRANT for the owner's grant command, REASON_MARKER for any other write to or
    use of the consent record / policy module, else None. Read-only commands pass."""
    if GRANT_COMMAND.search(command):
        return REASON_GRANT
    for word, seg in segments(command):
        if word in READ_ONLY:
            if _redirects_to_marker(seg):
                return REASON_MARKER
            continue
        if MARKER_FILE.search(seg):
            return REASON_MARKER
        if CONSENT_MODULE.search(seg) and not CONSENT_READ_CLI.match(seg):
            return REASON_MARKER
    return None


def _state():
    """``<workspace>/state`` via the consent module's resolver, or None when it cannot resolve."""
    try:
        return consent.state_dir()
    except Exception:
        return None


def _real(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def decide_file(path: str, state):
    """A file tool on the consent record: REASON_MARKER when ``path`` resolves (symlinks
    followed) to the consent marker or a denial marker; by name alone when state is unknown."""
    if not path:
        return None
    if state is None:
        return REASON_MARKER if MARKER_FILE.search(os.path.basename(path)) else None
    markers = [consent.consent_marker(state)] + [consent.denial_marker(a, state) for a in consent.APPS]
    target = _real(path)
    return REASON_MARKER if any(target == _real(m) for m in markers) else None


def consent_given(command: str, state) -> bool:
    if ESCAPE_HATCH.search(command) or consent.env_allows():
        return True
    return state is not None and consent.host_opted_in(state)


def decide(command: str, state):
    """The deny reason for this command, or None to allow."""
    reason = touches_consent(command)
    if reason:
        return reason
    if not targets_native_pim(command):
        return None
    if not consent_given(command, state):
        return REASON
    if state is not None:
        tier = consent.bound_task_tier(state)
        if tier is not None and tier != "owner":
            return REASON_NOT_OWNER_TASK
    return None


def main() -> None:
    data = json.loads(sys.stdin.read())
    tool = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool == "Bash":
        reason = decide(str(tool_input.get("command") or ""), _state())
    elif tool in FILE_TOOLS:
        reason = decide_file(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""),
                             _state())
    else:
        sys.exit(0)
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
