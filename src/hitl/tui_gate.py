"""A TUI gate as a HumanRequirement with semantic actions, and the keys that answer it.

The monitor (core-input-watch) sees a Claude Code dialog as text. This module is the
policy between that text and the card: which kind it is, which buttons it carries, and
— once a button is clicked — which keystrokes realise that click. The UI never names a
key; the card says `allow`/`deny`/`opt3`/`continue` and this module translates, after
re-checking that the dialog on screen is still the one the human saw.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Tuple

from .schema import Action, HumanRequirement

SOURCE = "tui"
FALLBACK_KIND = "core-blocked"
STALE_NOTE = "This prompt has changed. Refresh the action."
DID_NOT_TAKE_NOTE = "The answer did not take. Open the terminal to finish it."

_OPTION = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.*\S)\s*$")
_FRAME = re.compile(r"^[\s─━│┃┌┐└┘╭╮╰╯├┤┬┴┼═║╔╗╚╝]*$")
_HINT = re.compile(r"Esc to cancel|Enter to (confirm|select)|↑/↓|shift\+tab|Tab to", re.I)
_YES = re.compile(r"^\s*yes\b", re.I)
_NO = re.compile(r"^\s*no\b", re.I)


def guard_for(session: str, prompt: Optional[str], state: str) -> str:
    """The episode key: same session + same dialog text = same requirement."""
    return hashlib.sha256(f"{session}\n{prompt or state}".encode()).hexdigest()[:16]


def parse_options(prompt: Optional[str]) -> Tuple[List[str], Optional[int]]:
    """Numbered options in order, and the 0-based index the caret (❯) sits on."""
    options: List[str] = []
    caret: Optional[int] = None
    for line in (prompt or "").splitlines():
        m = _OPTION.match(line)
        if not m:
            continue
        if m.group(1):
            caret = len(options)
        options.append(m.group(3))
    return options, caret


def question(prompt: Optional[str], detail: str) -> str:
    """The lines a human would read as the question: above the options, frames and
    key hints dropped, the last three kept."""
    kept: List[str] = []
    for line in (prompt or "").splitlines():
        if _OPTION.match(line):
            break
        s = line.strip()
        if not s or _FRAME.match(s) or _HINT.search(s):
            continue
        kept.append(s)
    return " ".join(kept[-3:]) if kept else detail


def _open_terminal() -> Action:
    return Action(id="open_terminal", kind="open_terminal", label="Open terminal")


def requirement_for(state: str, gate: Optional[str], prompt: Optional[str],
                    session: str, detail: str, fallback_message: str) -> HumanRequirement:
    """Build the requirement a gate deserves. Unparseable dialogs fall back to the
    blocked card with no semantic button: a key is never guessed."""
    options, caret = parse_options(prompt)
    subject: Dict[str, object] = {"state": state, "gate": gate or "", "detail": detail,
                                  "session": session, "source": SOURCE,
                                  "options": options, "caret": caret}
    kind, title, message, actions = FALLBACK_KIND, f"{session} · {state}", fallback_message, []
    option_for_action: Dict[str, int] = {}

    if gate == "login" or state == "logged-out":
        kind, title = "auth", "Claude needs to be reconnected"
        message = "Your Claude session has expired. Sign in again to continue."
        actions = [Action(id="authenticate", kind="authenticate", label="Sign in to Claude")]
    elif gate == "press-enter":
        kind, title = "confirmation", "Agent needs your confirmation"
        message = question(prompt, detail)
        actions = [Action(id="continue", kind="continue", label="Continue")]
    elif options:
        # Kind follows the OPTIONS, not the gate label: classify() names a Yes/No
        # dialog `selection` whenever its caret row sits below the question.
        yes = next((i for i, o in enumerate(options) if _YES.match(o)), None)
        no = next((i for i, o in enumerate(options) if _NO.match(o)), None)
        message = question(prompt, detail)
        if yes is not None and no is not None:
            kind, title = "permission", "Agent needs your confirmation"
            option_for_action = {"deny": no, "allow": yes}
            actions = [Action(id="deny", kind="reject_once", label="No"),
                       Action(id="allow", kind="allow_once", label="Yes")]
        else:
            kind, title = "choice", "Agent needs a decision"
            option_for_action = {f"opt{i + 1}": i for i in range(len(options))}
            actions = [Action(id=f"opt{i + 1}", kind="select", label=o) for i, o in enumerate(options)]

    subject["option_for_action"] = option_for_action
    return HumanRequirement(
        kind=kind, runtime="claude", message=message, title=title,
        guard=guard_for(session, prompt, state),
        device={"id": session, "name": session},
        actions=actions + [_open_terminal()],
        subject=subject,
    )


def keys_for(req: HumanRequirement, current_prompt: Optional[str],
             current_state: str) -> Optional[List[str]]:
    """The keystrokes that realise `req.chosen_action` against the dialog on screen
    NOW, or None when the screen no longer shows the dialog the human clicked on."""
    subject = req.subject or {}
    if guard_for(str(subject.get("session") or ""), current_prompt, current_state) != req.guard:
        return None
    action = req.chosen_action
    if action == "continue":
        return ["Enter"]
    target = (subject.get("option_for_action") or {}).get(action)
    _, caret = parse_options(current_prompt)
    if target is None or caret is None:
        return None
    step = "Down" if target >= caret else "Up"
    return [step] * abs(target - caret) + ["Enter"]
