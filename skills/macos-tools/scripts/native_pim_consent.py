#!/usr/bin/env python3
"""Consent policy for the native Calendar / Reminders / Contacts apps — the one owner.

Driving those apps raises a macOS Automation permission prompt, so a script runs
only when the owner allowed the local app: ``--owner-asked`` on the command
line, ``SUTANDO_ALLOW_NATIVE_PIM=1`` in the environment, or the persisted host
opt-in ``<workspace>/state/native-pim-consent`` that the owner writes with
``python3 native_pim_consent.py grant`` in their own terminal (``revoke`` removes
it, ``status`` prints the state). A denial stored by macOS (``-1743``) is final:
it is recorded once in ``<workspace>/state/<app>-automation-denied``, later runs
skip the app without a retry, and ``grant`` clears those markers.

Every other reader delegates here: the macos-tools scripts import it, the
Bash hook (``hooks/native-pim-guard.py``) imports it for the marker names, the
host opt-in and the bound task's tier, the morning briefing imports it, and the
voice ``call_contact`` tool runs the ``check`` / ``report-error`` subcommands
(JSON on stdout). Inside an agent session (``CLAUDECODE=1``) the consent counts
only on the owner's own task: when ``state/bindings/active-execution.json`` names
the running task and its file resolves to a non-owner tier, every form of consent
is refused. A cron or a terminal run has no bound task.

This gate guards against the agent acting on its own initiative. It is not an
authorisation boundary: the flag and the env var are strings the caller writes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

FLAG = "--owner-asked"
ENV = "SUTANDO_ALLOW_NATIVE_PIM"
EXIT_NO_CONSENT = 2
EXIT_DENIED = 3
CONSENT_MARKER = "native-pim-consent"
DENIAL_MARKER_SUFFIX = "-automation-denied"
APPS = ("Calendar", "Reminders", "Contacts")
GRANT_COMMAND = "python3 skills/macos-tools/scripts/native_pim_consent.py grant"
BINDING_FILE = Path("state") / "bindings" / "active-execution.json"
_SRC = Path(__file__).resolve().parents[3] / "src"

ORDER = (
    "Order: (1) the Station connector first — composio_find {\"apps\": [\"google calendar\"]} "
    "then composio_exec with toolkit \"googlecalendar\" (google contacts / google tasks for "
    "Contacts and Reminders); (2) if not connected, the owner's own tools when they are in the "
    "tool list (mcp__claude_ai_Google_Calendar__*); (3) otherwise ask the owner. Never open the "
    "local app on your own, and never re-prompt once the owner denied the permission."
)


def _src_on_path() -> None:
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))


def state_dir() -> Path:
    """``<workspace>/state`` via the repo's workspace resolver (no private fallback)."""
    _src_on_path()
    from workspace_default import resolve_workspace
    return Path(resolve_workspace(migrate=False)) / "state"


def consent_marker(state: Path | None = None) -> Path:
    return (state_dir() if state is None else state) / CONSENT_MARKER


def denial_marker(app: str, state: Path | None = None) -> Path:
    return (state_dir() if state is None else state) / f"{app.lower()}{DENIAL_MARKER_SUFFIX}"


def env_allows() -> bool:
    return os.environ.get(ENV, "").strip() == "1"


def host_opted_in(state: Path | None = None) -> bool:
    """The host-level opt-in: the env var or the persisted consent marker."""
    return env_allows() or consent_marker(state).exists()


def owner_asked(argv: list[str] | None = None, state: Path | None = None) -> bool:
    argv = sys.argv if argv is None else argv
    return FLAG in argv or host_opted_in(state)


def strip_flag(argv: list[str]) -> list[str]:
    return [a for a in argv if a != FLAG]


def in_agent_session() -> bool:
    """A subprocess of the core agent session, where the task binding is about us."""
    return os.environ.get("CLAUDECODE", "") == "1"


def bound_task_tier(state: Path | None = None) -> str | None:
    """Tier of the task bound to the core session, or None when nothing is bound or readable."""
    state = state_dir() if state is None else state
    ws = state.parent
    try:
        with open(ws / BINDING_FILE) as f:
            task_id = str(json.load(f).get("task_id") or "")
    except (OSError, ValueError):
        return None
    if not task_id or "/" in task_id or task_id.startswith("."):
        return None
    task_file = ws / "tasks" / f"{task_id}.txt"
    if not task_file.exists():
        return None
    _src_on_path()
    try:
        from policy.egress.result import resolve_access_tier
    except Exception:
        return None
    return resolve_access_tier(task_file)


def not_owner_task(state: Path | None = None) -> bool:
    """True inside an agent session whose bound task is not owner-tier."""
    if not in_agent_session():
        return False
    tier = bound_task_tier(state)
    return tier is not None and tier != "owner"


def no_consent_message(app: str, *, cli: bool = True) -> str:
    """What the model must do instead; ``cli=False`` is the inline-tool wording (no flag)."""
    if not cli:
        return (
            f"The local macOS {app} app needs the owner to allow it once for this host (it raises "
            f"a permission prompt), so it was not opened. The owner allows it with "
            f"`{GRANT_COMMAND}` in their own terminal, or with SUTANDO_ALLOW_NATIVE_PIM=1 in the "
            f"server environment; until then, ask the owner for the details directly."
        )
    return (
        f"{app}: not read — the local macOS {app} app needs the owner to allow it "
        f"(it raises a permission prompt). Pass {FLAG} only when the owner asked for "
        f"the local app in this conversation, or the owner allows it once for this host "
        f"with `{GRANT_COMMAND}` in their own terminal. {ORDER}"
    )


def not_owner_task_message(app: str) -> str:
    return (
        f"{app}: not read — the task bound to this session is not the owner's, and the local "
        f"macOS {app} app raises a permission prompt on the owner's screen; {FLAG}, "
        f"{ENV}=1 and the host opt-in do not count on another person's task. {ORDER}"
    )


def require_consent(app: str, argv: list[str] | None = None,
                    state: Path | None = None) -> list[str]:
    """Exit ``EXIT_NO_CONSENT`` unless the owner asked on their own task; else argv without the flag."""
    argv = list(sys.argv if argv is None else argv)
    if not owner_asked(argv, state):
        print(no_consent_message(app), file=sys.stderr)
        sys.exit(EXIT_NO_CONSENT)
    if not_owner_task(state):
        print(not_owner_task_message(app), file=sys.stderr)
        sys.exit(EXIT_NO_CONSENT)
    return strip_flag(argv)


def is_denied(err: str) -> bool:
    lowered = (err or "").lower()
    return "-1743" in lowered or "not authorized to send apple events" in lowered


def denial_message(app: str) -> str:
    return (
        f"{app}: macOS denied automation access (System Settings → Privacy & Security → "
        f"Automation). Not retrying and not asking again — tell the owner, who can grant it "
        f"there and then run `{GRANT_COMMAND}` if they want the local app used."
    )


def record_denial(app: str, state: Path | None = None) -> Path:
    """Persist the macOS denial so no later run re-asks; unwritable state is not an error."""
    marker = denial_marker(app, state)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat())
    except OSError:
        pass
    return marker


def denied_earlier(app: str, state: Path | None = None) -> bool:
    return denial_marker(app, state).exists()


def exit_if_denied(app: str, err: str, state: Path | None = None) -> None:
    if is_denied(err):
        record_denial(app, state)
        print(denial_message(app), file=sys.stderr)
        sys.exit(EXIT_DENIED)


def exit_if_denied_earlier(app: str, state: Path | None = None) -> None:
    """A stored denial is final: say so and exit before any osascript runs."""
    if denied_earlier(app, state):
        print(denial_message(app), file=sys.stderr)
        sys.exit(EXIT_DENIED)


def check(app: str, state: Path | None = None) -> dict:
    """The decision for a tool that never asserts consent itself (the voice tools).

    A stored denial wins, then the host opt-in (env or marker). The tier binding
    is the core session's and is not applied here: the voice process gates the
    caller itself (owner-only tools).
    """
    if denied_earlier(app, state):
        return {"allowed": False, "reason": "denied", "message": denial_message(app)}
    if host_opted_in(state):
        return {"allowed": True, "reason": None, "message": None}
    return {"allowed": False, "reason": "no-consent", "message": no_consent_message(app, cli=False)}


def report_error(app: str, err: str, state: Path | None = None) -> dict:
    """Classify an osascript failure: a macOS denial is recorded and answered as final."""
    if not is_denied(err):
        return {"denied": False, "message": None}
    record_denial(app, state)
    return {"denied": True, "message": denial_message(app)}


def grant(state: Path | None = None) -> str:
    state = state_dir() if state is None else state
    state.mkdir(parents=True, exist_ok=True)
    consent_marker(state).write_text(datetime.now().isoformat())
    cleared = []
    for app in APPS:
        marker = denial_marker(app, state)
        if marker.exists():
            marker.unlink()
            cleared.append(marker.name)
    note = f"; cleared {', '.join(cleared)}" if cleared else ""
    return (f"native PIM allowed on this host ({consent_marker(state)}){note}. "
            f"macOS still asks once per app; deny there and the app stays off.")


def revoke(state: Path | None = None) -> str:
    marker = consent_marker(state)
    if marker.exists():
        marker.unlink()
        return f"native PIM consent removed ({marker})."
    return f"native PIM consent was not set ({marker})."


def status(state: Path | None = None) -> str:
    state = state_dir() if state is None else state
    lines = [
        f"consent marker: {'present' if consent_marker(state).exists() else 'absent'} "
        f"({consent_marker(state)})",
        f"{ENV}: {os.environ.get(ENV, '') or 'unset'}",
    ]
    for app in APPS:
        lines.append(f"{app}: {'DENIED by macOS (stored)' if denied_earlier(app, state) else 'not denied'}")
    return "\n".join(lines)


USAGE = (
    "Usage: python3 native_pim_consent.py [grant|revoke|status|check <App>|report-error <App> "
    "--error <text>] [--workspace <dir>] — the owner runs `grant` in their own terminal to allow "
    "the local Calendar/Reminders/Contacts apps."
)


def _take_option(argv: list[str], name: str) -> str | None:
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        return None
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    workspace = _take_option(argv, "--workspace")
    error = _take_option(argv, "--error")
    state = Path(workspace) / "state" if workspace else None
    cmd = argv[0] if argv else "status"
    app = argv[1] if len(argv) > 1 else ""
    if cmd == "grant":
        print(grant(state))
    elif cmd == "revoke":
        print(revoke(state))
    elif cmd == "status":
        print(status(state))
    elif cmd == "check" and app in APPS:
        print(json.dumps(check(app, state)))
    elif cmd == "report-error" and app in APPS:
        print(json.dumps(report_error(app, error or "", state)))
    else:
        print(USAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
