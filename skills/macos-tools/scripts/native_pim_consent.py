#!/usr/bin/env python3
"""Consent gate shared by the native Calendar / Reminders / Contacts scripts.

Driving those apps raises a macOS Automation permission prompt, so a script runs
only when the owner asked for the local app: ``--owner-asked`` on the command
line, or ``SUTANDO_ALLOW_NATIVE_PIM=1`` in the environment. A denial stored by
macOS (``-1743``) is final: the script says so once and exits without a retry.
"""
from __future__ import annotations

import os
import sys

FLAG = "--owner-asked"
ENV = "SUTANDO_ALLOW_NATIVE_PIM"
EXIT_NO_CONSENT = 2
EXIT_DENIED = 3

ORDER = (
    "Order: (1) the Station connector first — composio_find {\"apps\": [\"google calendar\"]} "
    "then composio_exec with toolkit \"googlecalendar\" (google contacts / google tasks for "
    "Contacts and Reminders); (2) if not connected, the owner's own tools when they are in the "
    "tool list (mcp__claude_ai_Google_Calendar__*); (3) otherwise ask the owner. Never open the "
    "local app on your own, and never re-prompt once the owner denied the permission."
)


def owner_asked(argv: list[str] | None = None) -> bool:
    argv = sys.argv if argv is None else argv
    return FLAG in argv or os.environ.get(ENV, "").strip() == "1"


def strip_flag(argv: list[str]) -> list[str]:
    return [a for a in argv if a != FLAG]


def require_consent(app: str, argv: list[str] | None = None) -> list[str]:
    """Exit ``EXIT_NO_CONSENT`` unless the owner asked; else return argv without the flag."""
    argv = list(sys.argv if argv is None else argv)
    if not owner_asked(argv):
        print(
            f"{app}: not read — the local macOS {app} app needs the owner to ask "
            f"(it raises a permission prompt). Pass {FLAG} only when the owner asked for "
            f"the local app in this conversation. {ORDER}",
            file=sys.stderr,
        )
        sys.exit(EXIT_NO_CONSENT)
    return strip_flag(argv)


def is_denied(err: str) -> bool:
    lowered = (err or "").lower()
    return "-1743" in lowered or "not authorized to send apple events" in lowered


def denial_message(app: str) -> str:
    return (
        f"{app}: macOS denied automation access (System Settings → Privacy & Security → "
        f"Automation). Not retrying and not asking again — tell the owner, who can grant it "
        f"there if they want the local app used."
    )


def exit_if_denied(app: str, err: str) -> None:
    if is_denied(err):
        print(denial_message(app), file=sys.stderr)
        sys.exit(EXIT_DENIED)
