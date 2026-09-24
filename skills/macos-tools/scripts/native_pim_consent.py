#!/usr/bin/env python3
"""Consent gate shared by the native Calendar / Reminders / Contacts scripts.

Driving those apps raises a macOS Automation permission prompt, so a script runs
only when the owner allowed the local app: ``--owner-asked`` on the command
line, ``SUTANDO_ALLOW_NATIVE_PIM=1`` in the environment, or the persisted host
opt-in ``<workspace>/state/native-pim-consent`` that the owner writes with
``python3 native_pim_consent.py grant`` in their own terminal (``revoke`` removes
it, ``status`` prints the state). A denial stored by macOS (``-1743``) is final:
it is recorded once in ``<workspace>/state/<app>-automation-denied``, later runs
skip the app without a retry, and ``grant`` clears those markers.

This gate guards against the agent acting on its own initiative. It is not an
authorisation boundary: the flag and the env var are strings the caller writes.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

FLAG = "--owner-asked"
ENV = "SUTANDO_ALLOW_NATIVE_PIM"
EXIT_NO_CONSENT = 2
EXIT_DENIED = 3
CONSENT_MARKER = "native-pim-consent"
APPS = ("Calendar", "Reminders", "Contacts")
GRANT_COMMAND = "python3 skills/macos-tools/scripts/native_pim_consent.py grant"

ORDER = (
    "Order: (1) the Station connector first — composio_find {\"apps\": [\"google calendar\"]} "
    "then composio_exec with toolkit \"googlecalendar\" (google contacts / google tasks for "
    "Contacts and Reminders); (2) if not connected, the owner's own tools when they are in the "
    "tool list (mcp__claude_ai_Google_Calendar__*); (3) otherwise ask the owner. Never open the "
    "local app on your own, and never re-prompt once the owner denied the permission."
)


def _config_dir_workspace() -> Path:
    p = os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    while True:
        if os.path.basename(p) == ".claude-sutando":
            return Path(os.path.dirname(p))
        parent = os.path.dirname(p)
        if parent == p:
            return Path(os.path.expanduser("~/sutando-workspace"))
        p = parent


def state_dir() -> Path:
    """``<workspace>/state`` — the repo resolver when importable, else the config-dir walk."""
    src = Path(__file__).resolve().parents[3] / "src"
    try:
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from workspace_default import resolve_workspace
        return Path(resolve_workspace()) / "state"
    except Exception:
        return _config_dir_workspace() / "state"


def consent_marker(state: Path | None = None) -> Path:
    return (state_dir() if state is None else state) / CONSENT_MARKER


def denial_marker(app: str, state: Path | None = None) -> Path:
    return (state_dir() if state is None else state) / f"{app.lower()}-automation-denied"


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


def no_consent_message(app: str) -> str:
    return (
        f"{app}: not read — the local macOS {app} app needs the owner to allow it "
        f"(it raises a permission prompt). Pass {FLAG} only when the owner asked for "
        f"the local app in this conversation, or the owner allows it once for this host "
        f"with `{GRANT_COMMAND}` in their own terminal. {ORDER}"
    )


def require_consent(app: str, argv: list[str] | None = None,
                    state: Path | None = None) -> list[str]:
    """Exit ``EXIT_NO_CONSENT`` unless the owner asked; else return argv without the flag."""
    argv = list(sys.argv if argv is None else argv)
    if not owner_asked(argv, state):
        print(no_consent_message(app), file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "grant":
        print(grant())
    elif cmd == "revoke":
        print(revoke())
    elif cmd == "status":
        print(status())
    else:
        print("Usage: python3 native_pim_consent.py [grant|revoke|status] — the owner runs "
              "`grant` in their own terminal to allow the local Calendar/Reminders/Contacts apps.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
