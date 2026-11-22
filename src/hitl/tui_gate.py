"""A TUI gate as a HumanRequirement with semantic actions, and the keys that answer it.
The card names an action; only this module knows which keys realise it on the live dialog."""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Sequence, Tuple

from . import recovery
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
_FRAME = re.compile(r"^[\s─━│┃┌┐└┘╭╮╰╯├┤┬┴┼═║╔╗╚╝╌╍┄┅┈┉]*$")
_EDGE = frozenset("─━═╭┌╔")  # a rule or a box top: where a dialog starts
_HINT = re.compile(r"Esc to cancel|Enter to (confirm|select)|↑/↓|shift\+tab|Tab to", re.I)
_FOOTER = re.compile(r"^⏵|←\s*for agents|esc to interrupt|\? for shortcuts", re.I)
_YES = re.compile(r"^\s*yes\b", re.I)
_NO = re.compile(r"^\s*no\b", re.I)


def guard_for(session: str, prompt: Optional[str], state: str) -> str:
    """The episode key: same session + same dialog = same requirement."""
    episode = dialog_identity(prompt) or prompt or state
    return hashlib.sha256(f"{session}\n{episode}".encode()).hexdigest()[:16]


def dialog_identity(prompt: Optional[str]) -> Optional[str]:
    """The live dialog's own text, from the rule or box top above it (an edit's inner diff box
    counts) to its last option, caret and borders dropped. Live = a caret block with only hints
    and footer below it; anything else returns None and the whole prompt keys the guard."""
    blocks = option_blocks(prompt)
    live = next((b for b in reversed(blocks) if b[2] is not None), None)
    if live is None:
        return None
    start, labels, _ = live
    lines = (prompt or "").splitlines()
    end = [i for i in range(start, len(lines)) if _OPTION.match(lines[i])][len(labels) - 1]
    for line in lines[end + 1:]:
        s = line.strip()
        if s and not (_FRAME.match(s) or _HINT.search(s) or _FOOTER.search(s)):
            return None  # a gate below the list: the list is not what it asks
    body: List[str] = []
    for line in reversed(lines[:start]):
        s = line.strip()
        if _OPTION.match(line) or s.startswith("✓") or s[:1] in _EDGE:
            break  # the dialog's top edge, or an older dialog above it
        s = s.strip("│┃║ ")
        if s and not _HINT.search(s):
            body.append(s)
    return "\n".join(body[::-1] + labels)


# A usage limit is one episode per seat however its screen repaints (banner, menu, refused
# turn): its cards share one guard, so every supervisor's card for it dedups onto one.
LIMIT_EPISODE = "usage-limit"


def limit_guard(session: str) -> str:
    return f"{LIMIT_EPISODE}:{session}"


def limit_card(req: HumanRequirement) -> bool:
    return (req.subject or {}).get("episode") == LIMIT_EPISODE


def limit_card_holds(req: HumanRequirement, pane: Optional[str], runtime: Optional[str] = None,
                     outrun: bool = False) -> bool:
    """A usage-limit card stays open while its limit stands on `pane`; every resolver asks this."""
    if not limit_card(req):
        return False
    from delivery.pane_gate import limit_standing
    return limit_standing(pane, runtime, outrun)


#: What Retry now does: the core's sends the refused message again; a worker's restarts it.
RETRY_LINE = "Retry now sends the refused message again."
RETRY_SEAT_LINE = "Retry now wakes the worker, and restarts it if it stays blocked."


def limit_unblock(session: Optional[str] = None, runtime: Optional[str] = None,
                  controls: Optional[Sequence[str]] = None, menu: bool = False) -> str:
    """How the owner lifts a usage limit, for the card body; the steps are the runtime's own.
    `controls`: the recovery ids the card carries, so the copy names those buttons and never a
    command or a purchase (the purchase is its own tagged button). `menu`: wait choices shown."""
    if controls:
        verbs = {(recovery.parse(a) or ("", None))[0] for a in controls}
        parts = ["It continues when the limit resets."]
        if menu:
            parts.append("Pick how to wait below.")
        what = " or ".join(w for v, w in ((recovery.SWITCH_MODEL, "model"), (recovery.SWITCH_RUNTIME, "runtime"))
                           if v in verbs)
        if what:
            parts.append(f"To keep working now, tap a {what} below that has usage left.")
        if recovery.RETRY_NOW in verbs:
            parts.append(RETRY_LINE)
        if recovery.ADD_ACCOUNT in verbs:
            parts.append("Another account with usage left also lifts it.")
        if recovery.RESTART_CORE in verbs:
            parts.append("Restart core starts it fresh.")
        return " ".join(parts)
    where = f"open its terminal (tmux session {session})" if session else "open its terminal"
    if runtime == "codex":
        return (f"It continues when the limit resets. To unblock it sooner, {where} and run "
                "`codex login` to switch to another account.")
    return (f"It continues when the limit resets. To unblock it sooner, {where} and run "
            "/usage-credits to use extra credits, /login to switch to another account, "
            "or /model to switch to a model with usage left.")


def sign_in_copy(runtime: Optional[str] = None, core: bool = False) -> Tuple[str, str, str]:
    """(title, message, button label) for a signed-out seat, in its own runtime's words.
    `core`: the core's own card, whose copy names its button, never a terminal command."""
    if runtime in (None, "claude"):
        return ("Claude needs you to sign in again",
                "Your Claude session has expired. Sign in again to continue.", "Sign in to Claude")
    if runtime == "codex" and core:
        return ("Codex needs you to sign in again",
                "Your Codex session has expired. Tap Sign in to Codex to continue.", "Sign in to Codex")
    if runtime == "codex":
        return ("Codex needs you to sign in again",
                "Your Codex session has expired. Run `codex login` in its terminal to continue.",
                "Sign in to Codex")
    return ("Agent needs you to sign in again",
            "The agent's sign-in has expired. Sign in again to continue.", "Sign in")


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


# How a card names each core runtime its Switch runtime button offers.
RUNTIME_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def recovery_action(aid: str, model_names: Optional[Dict[str, str]] = None) -> Action:
    return Action(id=aid, kind=recovery.kind_of(aid), label=recovery.label(aid, RUNTIME_NAMES, model_names))


# The only limit-menu choices a tap may choose: the runtime's own wait wordings, matched whole.
# Anything else (a key, overages, a login, a purchase, a switch, a rewording) is never one tap.
WAIT_CHOICES = (re.compile(r"^stop and wait for (?:the )?limit to reset$", re.I),
                re.compile(r"^wait here, then continue automatically at [\w ,:.]{1,40}$", re.I))


def safe_limit_options(options: Sequence[str]) -> List[Tuple[int, str]]:
    """(index, the runtime's own wording) for each limit-menu option a tap may choose: only the
    allowlisted wait choices, so tapping one is choosing it in the terminal."""
    out = []
    for i, option in enumerate(options):
        text = " ".join(option.split())
        if any(rx.match(text) for rx in WAIT_CHOICES):
            out.append((i, text))
    return out


def requirement_for(state: str, gate: Optional[str], prompt: Optional[str],
                    session: str, detail: str, fallback_message: str,
                    runtime: Optional[str] = None,
                    episode: Optional[str] = None,
                    controls: Optional[Sequence[str]] = None,
                    model_names: Optional[Dict[str, str]] = None,
                    guard: Optional[str] = None) -> HumanRequirement:
    """The requirement a gate deserves; unparseable or non-semantic gates keep the
    blocked card with no keystroke button, so a key is never guessed. A session with no stamped
    runtime predates the stamp and ran Claude, so `runtime` falls back to "claude".
    `episode` LIMIT_EPISODE keys the card on the seat, not the screen, and announces it once.
    `controls`: recovery action ids (hitl.recovery) a blocked card offers instead of a guessed
    keystroke; the caller picks them for the core or the seat. None keeps the card as it was.
    A limit menu also offers its wait-only choices as keystrokes (safe_limit_options).
    `guard`, when given, is the episode key the caller owns (hitl.core_card.episode_guard)."""
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

    if (episode == LIMIT_EPISODE or _NEVER_ONE_CLICK.search(prompt or "")
            or (gate is not None and gate not in SEMANTIC_GATES and state != "logged-out")):
        pass  # keeps the blocked card: never a one-click answer on a trust/spend gate or a limit
    elif gate == "login" or state == "logged-out":
        kind = "auth"
        title, message, label = sign_in_copy(runtime, core=controls is not None)
        actions = [Action(id="authenticate", kind="authenticate", label=label)]
    elif gate == "press-enter":
        kind, title = "confirmation", "Agent needs your confirmation"
        message = question(prompt, detail)
        actions = [Action(id="continue", kind="continue", label="Continue")]
    elif options and caret is not None:
        # Kind follows the OPTIONS: classify() names a Yes/No dialog `selection`.
        # No caret means keys_for can never answer it, so a button would only go stale.
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

    if episode == LIMIT_EPISODE and controls is not None and options and caret is not None:
        for i, label in safe_limit_options(options):
            option_for_action[f"opt{i + 1}"] = i
            actions.append(Action(id=f"opt{i + 1}", kind="select", label=label))
    if kind == FALLBACK_KIND and controls:
        actions += [recovery_action(aid, model_names) for aid in controls]
        subject["recovery"] = True
    subject["option_for_action"] = option_for_action
    if not guard:
        guard = limit_guard(session) if episode == LIMIT_EPISODE else guard_for(session, prompt, state)
    if episode == LIMIT_EPISODE:
        subject.update(episode=episode, remind=False)
    return HumanRequirement(
        kind=kind, runtime=runtime or "claude", message=message, title=title,
        guard=guard,
        device={"id": session, "name": session},
        actions=actions + [_open_terminal()],
        subject=subject,
    )


def keys_for(req: HumanRequirement, current_prompt: Optional[str],
             current_state: str) -> Optional[List[str]]:
    """The keystrokes that realise `req.chosen_action` against the dialog on screen
    NOW, or None when the screen no longer shows the dialog the human clicked on."""
    subject = req.subject or {}
    if limit_card(req):
        # A limit card is keyed on its seat, not its screen: the same menu must still be up.
        if parse_options(current_prompt)[0] != list(subject.get("options") or []):
            return None
    elif guard_for(str(subject.get("session") or ""), current_prompt, current_state) != req.guard:
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
