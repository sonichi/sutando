"""A TUI gate as a HumanRequirement with semantic actions, and the keys that answer it.
The card names an action; only this module knows which keys realise it on the live dialog."""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Tuple

from .schema import Action, HumanRequirement

SOURCE = "tui"
FALLBACK_KIND = "core-blocked"
STALE_NOTE = "This prompt has changed. Refresh the action."
DID_NOT_TAKE_NOTE = "The answer did not take. Open the terminal to finish it."

# Only these gates may carry a one-click button; trust, bypass and credit-spending
# dialogs keep the blocked card however their text is rendered.
SEMANTIC_GATES = frozenset({"permission", "selection", "press-enter", "login"})
# The classifier names the gate nearest the bottom, so a trust or credit dialog rendered
# as a numbered list arrives labelled `selection`; the TEXT still says what it is.
_NEVER_ONE_CLICK = re.compile(r"trust the files|Do you trust|Bypass Permissions|Yes, I accept|"
                              r"Fable limit|usage credits|hit your (?:session|usage|weekly) limit", re.I)
_OPTION = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.*\S)\s*$")
_FRAME = re.compile(r"^[\s─━│┃┌┐└┘╭╮╰╯├┤┬┴┼═║╔╗╚╝]*$")
_HINT = re.compile(r"Esc to cancel|Enter to (confirm|select)|↑/↓|shift\+tab|Tab to", re.I)
_YES = re.compile(r"^\s*yes\b", re.I)
_NO = re.compile(r"^\s*no\b", re.I)


def guard_for(session: str, prompt: Optional[str], state: str) -> str:
    """The episode key: same session + same dialog text = same requirement."""
    return hashlib.sha256(f"{session}\n{prompt or state}".encode()).hexdigest()[:16]


def option_blocks(prompt: Optional[str]) -> List[Tuple[int, List[str], Optional[int]]]:
    """Each contiguous run of numbered lines: (start line, labels, caret index)."""
    blocks: List[Tuple[int, List[str], Optional[int]]] = []
    cur: Optional[Tuple[int, List[str], Optional[int]]] = None
    for i, line in enumerate((prompt or "").splitlines()):
        m = _OPTION.match(line)
        if not m:
            if cur is not None and line.strip():
                blocks.append(cur)
                cur = None
            continue
        if cur is None:
            cur = (i, [], None)
        start, labels, caret = cur
        if m.group(1):
            caret = len(labels)
        labels.append(m.group(3))
        cur = (start, labels, caret)
    if cur is not None:
        blocks.append(cur)
    return blocks


def parse_options(prompt: Optional[str]) -> Tuple[List[str], Optional[int]]:
    """The LIVE dialog's options: the block holding the caret, else the last one.
    A resolved dialog still visible above the live one must not feed the buttons."""
    blocks = option_blocks(prompt)
    if not blocks:
        return [], None
    live = next((b for b in reversed(blocks) if b[2] is not None), blocks[-1])
    return live[1], live[2]


def question(prompt: Optional[str], detail: str) -> str:
    """The lines a human would read as the question: above the options, frames and
    key hints dropped, the last three kept."""
    kept: List[str] = []
    blocks = option_blocks(prompt)
    live_start = next((b[0] for b in reversed(blocks) if b[2] is not None), blocks[-1][0] if blocks else None)
    lines = (prompt or "").splitlines()
    for line in (lines[:live_start] if live_start is not None else lines):
        s = line.strip()
        if _OPTION.match(line) or s.startswith("✓"):
            kept = []  # an older dialog's options or its recorded answer: not this question
            continue
        if not s or _FRAME.match(s) or _HINT.search(s):
            continue
        kept.append(s)
    return " ".join(kept[-3:]) if kept else detail


def _open_terminal() -> Action:
    return Action(id="open_terminal", kind="open_terminal", label="Open terminal")


def requirement_for(state: str, gate: Optional[str], prompt: Optional[str],
                    session: str, detail: str, fallback_message: str) -> HumanRequirement:
    """The requirement a gate deserves; unparseable or non-semantic gates keep the
    blocked card with no button, so a key is never guessed."""
    options, caret = parse_options(prompt)
    subject: Dict[str, object] = {
        "session": session,
        "source": SOURCE,
        "supervisor_state": state,
        "gate": gate or "",
        "detail": detail,
        "options": options,
        "caret": caret,
    }
    kind, title, message, actions = FALLBACK_KIND, f"{session} · {state}", fallback_message, []
    option_for_action: Dict[str, int] = {}

    if _NEVER_ONE_CLICK.search(prompt or "") or (gate is not None and gate not in SEMANTIC_GATES and state != "logged-out"):
        pass  # keeps the blocked card: never a one-click answer on a trust/spend gate
    elif gate == "login" or state == "logged-out":
        kind, title = "auth", "Claude needs to be reconnected"
        message = "Your Claude session has expired. Sign in again to continue."
        actions = [Action(id="authenticate", kind="authenticate", label="Sign in to Claude")]
    elif gate == "press-enter":
        kind, title = "confirmation", "Agent needs your confirmation"
        message = question(prompt, detail)
        actions = [Action(id="continue", kind="continue", label="Continue")]
    elif options:
        # Kind follows the OPTIONS: classify() names a Yes/No dialog `selection`.
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
