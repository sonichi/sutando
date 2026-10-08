#!/usr/bin/env python3
"""Is a Claude CLI pane still signed out? One reading for every supervisor.

An expired login leaves one trace: the CLI answers the prompt with a single `⎿`
line ("Login expired · Please run /login", "Not logged in · Please run /login",
"OAuth access token has expired · Please run /login"), ends the turn in 0-1 s and
returns to its idle footer. No gate is on screen, so a monitor that reads "idle
footer, no gate" as recovery closes the owner's card while the seat is still
signed out.

Which lines ARE that refusal is not decided here. The banner grammar is
cli_wedge.py's `needs-login` live-banner family — the one grammar the seat
monitor's pane gate and the pool sweep already read a pane through — so a new
CLI wording is added there once. This module owns the reading over time: which
refusal still stands, and what clears it. Only positive evidence on the pane
clears the state:

- `login_expired(pane)` is the latest login-refusal line when nothing after it
  shows the CLI signed in again, else None. The words inside a turn that ran (a
  tool result quoting them, then the agent's own `●` line) are not a refusal.
- `authenticated_turn(pane)` is True only when no refusal stands AND the pane
  holds a completed turn that did work, or the CLI's "Login successful" line.
  A newer typed prompt, a spinner or an empty capture prove nothing.

- `signed_in_since(text)` is the clearing rule on its own: does `text`, the pane
  below a login marker, show "Login successful", the agent's `●` output, a `⏺`
  tool call, or a completed turn that outran a refusal (one finishes in 0-1 s
  with nothing but its `⎿` line)? runtime-health.py's `needs_login` finds its
  marker line with its own, broader marker set and reads "signed in after it"
  through this function, so the Console strip and the seat monitor never
  disagree about one pane.

A watcher re-arm or a task-status row is not readable from the pane, so that
evidence belongs to the pool sweep, not to this reading.
"""
from __future__ import annotations

import os.path as _osp
import re
import sys as _sys
from typing import List, Optional

_sys.path.insert(0, _osp.dirname(_osp.abspath(__file__)))
from cli_wedge import needs_login_line  # noqa: E402 — the one banner grammar

LOGGED_IN_AGAIN = re.compile(r"^login successful\b", re.I)
# The completed-turn line ("✻ Worked for 0s", "✻ Cooked for 1m 3s · done 12:32 PM"); the
# spinner reuses the glyph ("✻ Perambulating… (1m 46s · …)") and must not match.
TURN_DONE = re.compile(
    r"^✻\s+[A-Za-z]+(?:\s+for\s+(?P<dur>\d+[hms](?:\s+\d+[hms])*))?(?:\s*·\s*done\b.*)?$")
_SHORT = re.compile(r"[01]s")
_PROMPT = re.compile(r"^❯")
_RAN = re.compile(r"^[●⏺]")


def login_refusal(line: str) -> bool:
    """Is this one line the CLI's login refusal? cli_wedge's needs-login family, judged
    whole: the three common words without a /login token beside them are prose."""
    return needs_login_line(line)


def _core(line: str) -> str:
    return line.strip().lstrip("⎿").strip()


def _lines(pane: Optional[str]) -> List[str]:
    return [_core(ln) for ln in (pane or "").splitlines() if ln.strip()]


def _turn_start(lines: List[str], idx: int) -> int:
    return next((i for i in range(idx - 1, -1, -1) if _PROMPT.match(lines[i])), -1)


def _real_turn(lines: List[str], done: int) -> bool:
    """The completed turn ending at `done` did work: it outran a refusal, or the agent
    answered (`●`) or called a tool (`⏺`) in it."""
    dur = TURN_DONE.match(lines[done]).group("dur")
    if dur and not _SHORT.fullmatch(dur):
        return True
    return any(_RAN.match(ln) for ln in lines[_turn_start(lines, done) + 1:done])


def _signed_in_after(lines: List[str], idx: int) -> bool:
    for j in range(idx + 1, len(lines)):
        if LOGGED_IN_AGAIN.match(lines[j]) or _RAN.match(lines[j]):
            return True
        if TURN_DONE.match(lines[j]) and _real_turn(lines, j):
            return True
    return False


def signed_in_since(text: Optional[str]) -> bool:
    """Does `text`, the pane below a login marker, show the CLI signed in again?
    "Login successful", a `●`/`⏺` line or a completed turn that did work; nothing else."""
    return _signed_in_after(_lines(text), -1)


def login_expired(pane: Optional[str]) -> Optional[str]:
    """The refusal line the pane still stands on, or None.

    Latest refusal wins; it is void when it sits inside a turn that ran (a tool result
    quoting the words) or when anything after it shows the CLI signed in again."""
    lines = _lines(pane)
    last = max((i for i, ln in enumerate(lines) if login_refusal(ln)), default=None)
    if last is None:
        return None
    if any(_RAN.match(ln) for ln in lines[_turn_start(lines, last) + 1:last]):
        return None
    return None if _signed_in_after(lines, last) else lines[last]


def auth_expired(pane: Optional[str]) -> bool:
    return login_expired(pane) is not None


def authenticated_turn(pane: Optional[str]) -> bool:
    """Positive proof the session is signed in: no refusal stands, and the pane holds
    "Login successful" or a completed turn that did work. Absence of evidence is False."""
    lines = _lines(pane)
    if login_expired(pane) is not None:
        return False
    return any(LOGGED_IN_AGAIN.match(ln) or (TURN_DONE.match(ln) and _real_turn(lines, i))
               for i, ln in enumerate(lines))
