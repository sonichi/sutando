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

- `refusal_answered(pane)` is True when a refusal is on the pane and something after it
  shows the CLI signed in again: the first frame that proves the refusal is over.

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
from typing import List, Optional, Tuple

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
# Codex: a response or tool step is a `•` line at column 0; the spinner shares the glyph.
CODEX = "codex"
_CODEX_RAN = re.compile(r"^•\s+\S")
_CODEX_BUSY = re.compile(r"^•\s+Working\b|esc to interrupt", re.I)
_CODEX_PROMPT = re.compile(r"^[›»]")


def login_refusal(line: str) -> bool:
    """Is this one line the CLI's login refusal? cli_wedge's needs-login family, judged
    whole: the three common words without a /login token beside them are prose."""
    return needs_login_line(line)


def _core(line: str) -> str:
    return line.strip().lstrip("⎿").strip()


def _rows(pane: Optional[str]) -> List[Tuple[str, str]]:
    """(core, raw) per non-blank line: the raw keeps the indent a quoted line carries."""
    return [(_core(ln), ln.rstrip()) for ln in (pane or "").splitlines() if ln.strip()]


def _lines(pane: Optional[str]) -> List[str]:
    return [c for c, _r in _rows(pane)]


def _ran(row: Tuple[str, str], runtime: Optional[str]) -> bool:
    core, raw = row
    if runtime == CODEX:
        return bool(_CODEX_RAN.match(raw)) and not _CODEX_BUSY.search(raw)
    return bool(_RAN.match(core))


def _is_prompt(core: str, runtime: Optional[str]) -> bool:
    return bool(_PROMPT.match(core) or (runtime == CODEX and _CODEX_PROMPT.match(core)))


def _turn_start(rows, idx: int, runtime: Optional[str] = None) -> int:
    return next((i for i in range(idx - 1, -1, -1) if _is_prompt(rows[i][0], runtime)), -1)


def _real_turn(rows, done: int) -> bool:
    """The completed turn ending at `done` did work: it outran a refusal, or the agent
    answered (`●`) or called a tool (`⏺`) in it."""
    dur = TURN_DONE.match(rows[done][0]).group("dur")
    if dur and not _SHORT.fullmatch(dur):
        return True
    return any(_ran(r, None) for r in rows[_turn_start(rows, done) + 1:done])


def _signed_in_after(rows, idx: int, runtime: Optional[str] = None) -> bool:
    for j in range(idx + 1, len(rows)):
        if LOGGED_IN_AGAIN.match(rows[j][0]) or _ran(rows[j], runtime):
            return True
        if runtime != CODEX and TURN_DONE.match(rows[j][0]) and _real_turn(rows, j):
            return True
    return False


def signed_in_since(text: Optional[str], runtime: Optional[str] = None) -> bool:
    """Does `text`, the pane below a login marker, show the CLI signed in again?
    "Login successful", the agent's own output, or a completed turn that did work."""
    return _signed_in_after(_rows(text), -1, runtime)


def _last_refusal(rows) -> Optional[int]:
    return max((i for i, (c, _r) in enumerate(rows) if login_refusal(c)), default=None)


def login_expired(pane: Optional[str], runtime: Optional[str] = None) -> Optional[str]:
    """The refusal line the pane still stands on, or None.

    Latest refusal wins; it is void when it sits inside a turn that ran (a tool result
    quoting the words) or when anything after it shows the CLI signed in again."""
    rows = _rows(pane)
    last = _last_refusal(rows)
    if last is None:
        return None
    if any(_ran(r, runtime) for r in rows[_turn_start(rows, last, runtime) + 1:last]):
        return None
    return None if _signed_in_after(rows, last, runtime) else rows[last][0]


def auth_expired(pane: Optional[str], runtime: Optional[str] = None) -> bool:
    return login_expired(pane, runtime) is not None


def refusal_answered(pane: Optional[str], runtime: Optional[str] = None) -> bool:
    """A login refusal is on the pane and the CLI signed in after it ("Login successful",
    the agent's own output, or a completed turn that did work), in this one frame."""
    rows = _rows(pane)
    last = _last_refusal(rows)
    return last is not None and _signed_in_after(rows, last, runtime)


def authenticated_turn(pane: Optional[str], runtime: Optional[str] = None) -> bool:
    """Positive proof the session is signed in: no refusal stands, and the pane holds
    "Login successful" or a completed turn that did work. Absence of evidence is False.
    Codex has no done line: its turn is complete when a prompt follows a `•` response."""
    rows = _rows(pane)
    if login_expired(pane, runtime) is not None:
        return False
    if any(LOGGED_IN_AGAIN.match(c) for c, _r in rows):
        return True
    if runtime == CODEX:
        return any(_ran(r, runtime) and any(_is_prompt(c, runtime) for c, _x in rows[i + 1:])
                   for i, r in enumerate(rows))
    return any(TURN_DONE.match(c) and _real_turn(rows, i) for i, (c, _r) in enumerate(rows))
