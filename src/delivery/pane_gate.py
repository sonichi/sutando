#!/usr/bin/env python3
"""Pane idle-gate and line delivery for a core CLI pane — the consumer-side policy every external task-notifier shares.

A notifier answers two questions before it types into the core's tmux pane: is
the pane ready to receive a line, and how does the line get in. This module owns
both answers for every runtime; a notifier keeps only its own I/O (which pane it
captures, how often it polls, what it logs).

    classify_pane(text, ADAPTERS["codex"])   -> Verdict(state, reason, pending)
        state: idle-ready | busy | pending | abnormal | unknown   (unknown is never idle)
    deliver(line, session, "codex", ...)     -> Outcome mapped from tmux-send-line.sh's exit code

Runtime specifics are DATA on a RuntimeAdapter — prompt glyph, idle footer, gate
signatures, input-affordance hint, placeholder — never branches in the
policy. A gate signature counts only while a current input affordance is on screen
and no empty composer sits under it: a finished turn's prose may say "Select" or
"Press Enter to continue", and above the runtime's own empty prompt it is history.
The Claude constants live here and core-input-watch.py aliases them; the
prompt-line parse lives here and scripts/tmux-send-line.sh calls it.

Boundary: cli_wedge.py stays advisory — it reports idle/moving x healthy/abnormal
and never decides "send". This gate decides, and only deliver() reaches the one
sender, scripts/tmux-send-line.sh. Nothing here calls send-keys.

CLI:
    python3 src/delivery/pane_gate.py classify --runtime codex [--json] < capture.txt
    python3 src/delivery/pane_gate.py pending  --runtime codex < capture.txt
    python3 src/delivery/pane_gate.py composer-text --runtime claude < capture.txt
    python3 src/delivery/pane_gate.py leftover --runtime codex < capture.txt   # exit 0 = clearable
    python3 src/delivery/pane_gate.py healthy --runtime claude < capture.txt   # exit 0 = accepts input
    python3 src/delivery/pane_gate.py deliver <session> <line> --runtime codex
        [--socket PATH] [--refuse-if-pending] [--skip-if-queued WORD] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

_SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SRC))
from cli_wedge import (PROVIDER_LIMIT_PATTERNS, FrameAbnormal, capture_pane,  # noqa: E402
                       core_target, frame_abnormal, frame_working, needs_login_line, normalize,
                       state_id)
from quota_availability import provider_allows_now  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from worker_auth_state import signed_in_since  # noqa: E402

REPO = _SRC.parent
SEND_LINE = REPO / "scripts" / "tmux-send-line.sh"

#: Non-blank lines a verdict reads — the same window core-input-watch.py reads.
TAIL_LINES = 14

#: A CSI SGR run (colon or semicolon params, newer tmux emits both) or an OSC
#: hyperlink (BEL- or ST-terminated) -- anything else is left as real content.
_SGR = re.compile(r"\x1b\[[0-9:;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
#: Public alias for strip-to-plain outside this module (core-input-watch.py's
#: own derivation) -- one pattern, not a second copy that can drift.
SGR_STRIP = _SGR
# Dim (SGR 2) or the 256-colour GREYSCALE RAMP (232-255): styled runs are hint/ghost
# text only. 200-231 are colour-cube entries, so real coloured input must not match.
_GHOST = re.compile(
    r"\x1b\[(?:2|38;5;(?:23[2-9]|24[0-9]|25[0-5]))m.*?(?=\x1b\[(?:0|22|39)m|$)")

STATES = ("idle-ready", "busy", "pending", "abnormal", "unknown")

# The one owner of "may something type into this pane"; callers ask, never restate.
# `unknown` refuses: a pane that could not be parsed may hold an unsent draft.
UNSAFE_STATES = ("pending", "busy", "abnormal", "unknown")

EXIT_UNSAFE = 3  # `safe` said no, or `pending` found no prompt at all


def state_is_unsafe(state: str) -> bool:
    """True when `state` must not be typed into. Unrecognised states refuse too."""
    return state in UNSAFE_STATES or state not in STATES

# ---- Claude Code pane text. core-input-watch.py imports these three; edit here only.
FABLE_TEXT = re.compile(r"reached your Fable limit|included Fable usage for this week", re.I)
CLAUDE_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|←\s*for agents\b", re.I)
# The CLI's wait-for-the-reset banner ("Usage limit reached · …", "Continuing automatically at …").
LIMIT_WAIT = re.compile(r"Continuing automatically at\b|\b(?:session|usage|weekly) limit reached\b", re.I)
# A completed turn ("✻ Cooked for 1s · done 12:32 PM"); the spinner reuses the glyph and must not match.
TURN_DONE = re.compile(
    r"^\s*✻\s+[A-Za-z]+(?:\s+for\s+(?P<dur>\d+[hms](?:\s+\d+[hms])*))?(?:\s*·\s*done\b.*)?\s*$")
#: A turn this short (or with no duration) may be the CLI refusing it, not running it.
TURN_SHORT = re.compile(r"[01]s")
# A turn the CLI refuses outright is a FINISHED turn: the reason is its `⎿` result and the
# pane returns to the idle footer, so no gate is on screen. Explicit list, extended by hand.
LIMIT_REFUSAL = re.compile(
    r"out of usage credits|/usage-credits|hit your (?:session|usage|weekly) limit", re.I)
REFUSAL = re.compile(
    LIMIT_REFUSAL.pattern
    # A bare "not logged in" is a refusal only as the CLI's own line-start `⎿` row.
    + r"|^⎿?\s*(?:you(?:'re| are) )?not logged in\b", re.I)


def refusal_line(line: str) -> bool:
    """A limit refusal, or the CLI's needs-login line (cli_wedge's one grammar)."""
    return bool(REFUSAL.search(line)) or needs_login_line(line)
# First match classifies; the idle "❯ " prompt matches none of them.
CLAUDE_GATE_SIGNATURES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("fable-limit", re.compile(r"❯\s*Switch to .{1,80}? and continue", re.I)),
    ("fable-limit-unfocused", FABLE_TEXT),
    ("session-limit", re.compile(r"hit your (?:session|usage|weekly) limit", re.I)),
    ("usage-limit", LIMIT_WAIT),
    # Codex 0.160 asks "Trust this folder?"; Claude Code, "Do you trust the files in this folder?".
    ("folder-trust", re.compile(r"trust the files in this folder|Do you trust|Trust this folder\?", re.I)),
    ("bypass-permissions", re.compile(r"Bypass Permissions mode|Yes, I accept", re.I)),
    ("login", re.compile(r"Select login method|Paste code here|Browser didn'?t open", re.I)),
    ("press-enter", re.compile(r"Press Enter to continue", re.I)),
    ("selection", re.compile(r"(❯\s*\d+\.|\bSelect\b).*", re.S)),
    ("permission", re.compile(r"Do you want to (proceed|allow)|Allow this action|permission to", re.I)),
]
# The gate kinds above that are a usage limit (the wait banner, the limit menu).
LIMIT_GATES = frozenset({"session-limit", "usage-limit"})


def limit_line(line: str) -> bool:
    """One line of the limit's own words: the wait banner, the menu title or a refusal."""
    return bool(LIMIT_WAIT.search(line) or LIMIT_REFUSAL.search(line))


def limit_standing(pane: Optional[str], runtime: Optional[str] = None, outrun: bool = False) -> bool:
    """A limit line is on the pane and no turn did work after it. A delivery the limit
    refuses repaints the pane without ending the episode; only work or no limit text does.
    `outrun`: the caller judged the banner under the composer stale, so it is not evidence."""
    if not (pane and pane.strip()):
        return True  # no evidence either way: the episode stands
    lines = (strip_limit_banners(pane) if outrun else pane).splitlines()
    last = max((i for i, ln in enumerate(lines) if limit_line(ln)), default=None)
    return last is not None and not signed_in_since("\n".join(lines[last + 1:]), runtime)


_NUMBERED_ROW = re.compile(r"^\s*(?:❯\s*)?\d+\.\s")
_CARET_ROW = re.compile(r"^\s*❯\s*\d+\.\s")


def _dialog_names_limit(text: str) -> bool:
    """The live numbered dialog's own text, back to the turn above it, names a limit."""
    lines = (text or "").splitlines()
    rows = [i for i, ln in enumerate(lines) if _CARET_ROW.match(ln)] or \
        [i for i, ln in enumerate(lines) if _NUMBERED_ROW.match(ln)]
    if not rows:
        return False
    start = rows[-1]
    while start > 0 and _NUMBERED_ROW.match(lines[start - 1]):
        start -= 1
    for line in reversed(lines[:start]):
        core = line.strip()
        if not core:
            continue
        if core[0] in "✻⎿●⏺" or (core.startswith("❯") and core.lstrip("❯").strip()):
            return False  # the turn above the dialog: its words are not the dialog's
        if limit_line(core):
            return True
    return False


def is_usage_limit(kind: Optional[str], text: Optional[str]) -> bool:
    """`kind` (a gate signature, or turn-rejected) on `text` waits on a usage limit: the
    wait banner, the limit menu (classified `selection`, its caret row being lowest) or a
    turn refused for a limit."""
    if kind in LIMIT_GATES:
        return True
    if kind == "turn-rejected":
        return bool(LIMIT_REFUSAL.search(text or ""))
    return kind in ("selection", "unknown") and _dialog_names_limit(text or "")


# A dialog that holds the input shows how to answer it: a caret on a numbered row or a key hint.
AWAIT_HINT = re.compile(
    r"Esc to cancel|Enter to confirm|Enter to select|to navigate|Press Enter|Paste code|to accept"
    r"|Continuing automatically|❯\s*\d+\.", re.I)
# A turn's spinner ("✳ Moonwalking… (15s · …)") or completion line: transcript above, live dialog
# below. Not "esc to interrupt": the busy footer carries it, under any live dialog.
_SPINNER_ROW = re.compile(r"^\s*[·✢✳✶✻✽*]\s+[A-Za-z][\w-]*(?:…|\.\.\.)\s*\(")
_TURN_ROW = re.compile(rf"{_SPINNER_ROW.pattern}|{TURN_DONE.pattern}")


def live_tail(lines: List[str]) -> List[str]:
    """`lines` below the last turn spinner / completion row, else all of them. Tool output
    above that row may quote a dialog's caret or key hints; only a dialog below it is live."""
    for i in range(len(lines) - 1, -1, -1):
        if _TURN_ROW.search(_SGR.sub("", lines[i])):
            return lines[i + 1:]
    return lines


@dataclass(frozen=True)
class RuntimeAdapter:
    name: str
    glyph: str
    idle_ready: "re.Pattern[str]"
    gate_signatures: Tuple[Tuple[str, "re.Pattern[str]"], ...]
    await_hint: "re.Pattern[str]" = AWAIT_HINT
    alternate_glyphs: Tuple[str, ...] = ()
    idle_requires_prompt: bool = False
    # Composer text only automation ever types: a dispatcher may clear exactly these.
    automation_leftovers: Tuple[str, ...] = ()
    # The runtime's dispatcher waits for idle-ready, so a draft holds every queued task.
    pending_blocks_dispatch: bool = False


CLAUDE = RuntimeAdapter(
    name="claude", glyph="❯", idle_ready=CLAUDE_IDLE,
    gate_signatures=tuple(CLAUDE_GATE_SIGNATURES),
)
# Codex's composer glyph varies by CLI version; its model row is the idle footer.
CODEX_IDLE = re.compile(
    rf"{CLAUDE_IDLE.pattern}|^[ \t]*(?:gpt-[\w.-]+|codex[\w.-]*|o[1-9][\w.-]*)\b[^\n]*[·•]",
    re.I | re.M,
)
CODEX_PICKER_ROW = re.compile(r"[›»]\s*\d+\.")
CODEX = RuntimeAdapter(
    name="codex", glyph="›", idle_ready=CODEX_IDLE,
    gate_signatures=tuple(CLAUDE_GATE_SIGNATURES) + (("selection", CODEX_PICKER_ROW),),
    await_hint=re.compile(f"{AWAIT_HINT.pattern}|{CODEX_PICKER_ROW.pattern}", re.I),
    alternate_glyphs=("»",),
    idle_requires_prompt=True,
    # Codex rejects Claude's /startup and leaves it in the composer.
    automation_leftovers=("/startup",),
    pending_blocks_dispatch=True,
)
ADAPTERS = {CLAUDE.name: CLAUDE, CODEX.name: CODEX}


@dataclass(frozen=True)
class PromptLine:
    text: str
    placeholder: bool


@dataclass(frozen=True)
class Verdict:
    state: str
    reason: str
    pending: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Outcome:
    status: str
    code: int
    message: str


def _prompt_glyph(raw: str, adapter: RuntimeAdapter) -> Optional[str]:
    plain = _SGR.sub("", raw).lstrip(" \t")
    return next((glyph for glyph in (adapter.glyph, *adapter.alternate_glyphs)
                 if plain.startswith(glyph)), None)


def prompt_line(capture: str, adapter: RuntimeAdapter, width: int = 0) -> Optional[PromptLine]:
    """The LAST line starting with the runtime's glyph (scrollback holds old ones):
    its input is what follows the glyph and one optional space/nbsp, with any
    styled hint (dim placeholder, grey ghost suggestion) dropped first -- typed
    text is unstyled on both runtimes, so a styled run after the glyph is never
    pending. None = no prompt seen."""
    lines_ = capture.splitlines()
    last_i, found = -1, None
    for i, raw in enumerate(lines_):
        glyph = _prompt_glyph(raw, adapter)
        if glyph is None:
            continue
        rest_raw = raw[raw.find(glyph) + len(glyph):]
        rest_ghostless, n = _GHOST.subn("", rest_raw)
        rest = _SGR.sub("", rest_ghostless)
        placeholder = n > 0
        if rest[:1] in (" ", "\u00a0"):
            rest = rest[1:]
        last_i, found, last_raw = i, PromptLine(rest.rstrip(), placeholder), rest_raw
    if found is None or width <= 0:
        return found
    raw_parts = [last_raw]
    prev_full = len(_SGR.sub("", lines_[last_i])) >= width
    for nxt in lines_[last_i + 1:]:
        if not prev_full:
            break
        plain = _SGR.sub("", nxt)
        if _prompt_glyph(nxt, adapter) is not None:
            break
        raw_parts.append(nxt)
        prev_full = len(plain) >= width
    # A ghost run opened on the prompt row stays open across the wrap, and its continuation
    # rows carry no SGR of their own -- strip AFTER joining, or the tail reads as input.
    joined_ghostless, n = _GHOST.subn("", "".join(raw_parts))
    joined = _SGR.sub("", joined_ghostless)
    if joined[:1] in (" ", " "):
        joined = joined[1:]
    return PromptLine(joined.rstrip(), n > 0)


def pending_text(capture: str, adapter: RuntimeAdapter, width: int = 0) -> Optional[str]:
    """Text typed at the current prompt; "" when the composer is empty; None when no prompt is seen."""
    line = prompt_line(capture, adapter, width)
    return None if line is None else line.text


def after_prompt(capture: str, adapter: RuntimeAdapter, width: int = 0) -> str:
    """Everything BELOW the current prompt line, colour stripped -- a fingerprint of the
    rest of the pane. A prompt line matching a staged payload is not proof the composer
    is still live: it can be a stale line from before a gate appeared beneath it. Callers
    doing a delayed re-check (tmux-send-line.sh's Codex path) must also require this be
    unchanged, not just the prompt text. "" when no prompt line was found at all."""
    lines_ = capture.splitlines()
    last_i = -1
    for i, raw in enumerate(lines_):
        if _prompt_glyph(raw, adapter) is not None:
            last_i = i
    if last_i < 0:
        return ""
    end = last_i
    if width > 0:
        # A wrapped composer continues onto rows with no glyph; they are input, not content
        # below it, so the fingerprint must start after the last continuation row.
        prev_full = len(_SGR.sub("", lines_[last_i])) >= width
        for j in range(last_i + 1, len(lines_)):
            if not prev_full:
                break
            plain = _SGR.sub("", lines_[j])
            if _prompt_glyph(lines_[j], adapter) is not None:
                break
            end = j
            prev_full = len(plain) >= width
    return "\n".join(_SGR.sub("", x).strip() for x in lines_[end + 1:])


#: A pane-border/rule row (box-drawing chars only) -- never legitimate composer text.
BORDER_LINE = re.compile(r"^[\s─-╿]+$")
# The CLI's hint in an EMPTY composer (`❯ Try "refactor <filepath>"`); it vanishes
# on the first typed character, so it is never a draft. Plain capture loses its dimming.
COMPOSER_PLACEHOLDER = re.compile(
    r'^\s*❯\s*(?:Try "[^"\n]*"|Press up to edit queued messages)\s*$')
# A single hint line the CLI prints below its own idle footer; only the fixed
# "Tip:" lead-in is matched, since the tip text itself rotates release to release.
TIP_ROW = re.compile(r"Tip:\s")


def composer_text(capture: str, adapter: RuntimeAdapter = CLAUDE) -> Optional[str]:
    """The composer's full typed content, dewrapped, or None with no <glyph> line
    at all; "" for an empty composer. The ONE parser for task-notifier.sh's
    EXACT-equality staging checks -- core-input-watch.py's `_composer_text`
    aliases this rather than re-deriving it.

    From the bottommost prompt line to the end of the capture. Below an editable
    composer the CLI renders its own frame -- box rule(s), at most one idle-footer
    row, at most one hint/tip row -- so the strip removes exactly those, once each,
    from the back. It never re-classifies an interior row: an owner's own typed
    line that happens to read one of those rows' words survives, because only the
    LAST matching row of each kind is ever popped, and never a second time. The cut
    at the closing rule follows the same rule: the LAST rule row, never an interior one.
    """
    lines = [ln for ln in capture.splitlines() if ln.strip()]
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if _prompt_glyph(lines[i], adapter) is not None:
            start = i
            break
    if start is None:
        return None
    block = lines[start:]
    # The CLI boxes the composer: everything past its closing rule is frame (footer, tip,
    # "⧉ <artifact>" strip). The LAST rule is the closing one; an interior rule is typed text.
    for i in range(len(block) - 1, 0, -1):
        if BORDER_LINE.match(block[i]):
            block = block[:i]
            break

    def _pop_borders():
        while len(block) > 1 and BORDER_LINE.match(block[-1]):
            block.pop()

    _pop_borders()
    # A tip row renders below the idle-footer row, so it is popped first --
    # else the idle-footer check below never reaches its own trailing row.
    if len(block) > 1 and TIP_ROW.search(block[-1]):
        block.pop()
    _pop_borders()
    if len(block) > 1 and adapter.idle_ready.search(block[-1]):
        block.pop()
    _pop_borders()
    if not block or (len(block) == 1 and COMPOSER_PLACEHOLDER.match(block[0])):
        return ""
    block[0] = prompt_line(block[0], adapter).text
    return "".join(block)


def composer_frame_visible(capture: str, adapter: RuntimeAdapter = CLAUDE) -> Optional[bool]:
    """Whether the CLI's frame below the composer (its closing rule, the idle footer) is
    on screen; None with no <glyph> line. False = the box is cut by the screen bottom, so
    its last rows are unseen and no capture can show where the typed text ends."""
    lines = [ln for ln in capture.splitlines() if ln.strip()]
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if _prompt_glyph(lines[i], adapter) is not None:
            start = i
            break
    if start is None:
        return None
    for row in lines[start + 1:]:
        if BORDER_LINE.match(row) or adapter.idle_ready.search(row):
            return True
    return False


def _tail_lines(capture: str) -> List[str]:
    """The last TAIL_LINES non-blank lines, attributes kept (the composer parse needs them)."""
    return [ln for ln in capture.splitlines() if _SGR.sub("", ln).strip()][-TAIL_LINES:]


def gate_evidence(tail: str, styled_tail: Optional[str] = None,
                  adapter: RuntimeAdapter = CLAUDE) -> str:
    """Exclude idle chrome, verified ghost suggestions and completed conditional recaps."""
    rows = tail.splitlines()
    line = prompt_line(styled_tail if styled_tail is not None else tail, adapter)
    composer_i = next((i for i in range(len(rows) - 1, -1, -1)
                       if _prompt_glyph(rows[i], adapter) is not None), None)
    completed = (composer_i is not None and line is not None and not line.text.strip()
                 and any(row.strip() == "Nothing needs you this round."
                         for row in rows[:composer_i]))
    kept = []
    for i, row in enumerate(rows):
        signatures = any(rx.search(row) for _, rx in adapter.gate_signatures)
        if adapter.idle_ready.search(row) and not signatures:
            continue
        if i == composer_i and line and line.placeholder and not line.text.strip():
            continue
        # A marker alone cannot distinguish a live instruction from completed output.
        if (completed and i < composer_i and not signatures
                and re.fullmatch(r"\s*※ recap: if .+, press Enter there to send it\.\s*", row)
                and len(list(adapter.await_hint.finditer(row))) == 1):
            continue
        kept.append(row)
    return "\n".join(kept)


def _gate(capture: str, tail: str, line: Optional[PromptLine], adapter: RuntimeAdapter) -> Optional[str]:
    """The gate kind holding the pane, or None. An empty composer at the bottom means no
    dialog owns the input -- UNLESS a gate has appeared BELOW that composer line since it
    was drawn. A stale empty composer sitting above a freshly-printed gate is not proof the
    gate is gone (keweichen/round-4, 2026-09-17): a login/approval dialog can render under
    an already-idle footer within the same capture. Check after_prompt() for exactly that
    case -- everything below the current prompt line -- before trusting the composer."""
    if line is not None and not line.text:
        following = after_prompt(capture, adapter)
        # A live pane draws its footer last, so text after the LAST footer line is
        # newer than the footer: a dialog there is live even under a trailing glyph.
        footer_hits = list(adapter.idle_ready.finditer(tail))
        beyond_footer = tail[footer_hits[-1].end():] if footer_hits else ""
        newer = "\n".join(part for part in (following, beyond_footer) if part.strip())
        newer = gate_evidence(newer, adapter=adapter)
        if not newer or not adapter.await_hint.search(newer):
            return None
        for kind, rx in adapter.gate_signatures:
            if rx.search(newer):
                return kind
        # An await hint we cannot name is an UNRECOGNISED dialog, not the absence of one.
        # Falling through to "no gate" let a stale empty composer vouch for a live dialog.
        return "unlisted"
    tail = gate_evidence(tail, "\n".join(_tail_lines(capture)), adapter)
    if not adapter.await_hint.search(tail):
        return None
    for kind, rx in adapter.gate_signatures:
        if rx.search(tail):
            return kind
    return None


def automation_leftover(capture: Optional[str], adapter: RuntimeAdapter, workspace=None,
                        socket=None, session=None, verdict: Optional[Verdict] = None) -> Optional[str]:
    """The composer's text when it is EXACTLY one of the runtime's automation leftovers and
    nothing else, else None. Both parsers must agree: the prompt row alone misses a
    multi-line draft whose first row happens to read the same, and the composer's frame must
    be on screen so no row of it is hidden."""
    if not adapter.automation_leftovers or capture is None:
        return None
    v = verdict or classify_pane(capture, adapter, workspace, socket, session)
    if v.state != "pending" or v.pending not in adapter.automation_leftovers:
        return None
    if not composer_frame_visible(capture, adapter):
        return None  # the box runs off the screen bottom: more text may sit below
    return v.pending if composer_text(capture, adapter) == v.pending else None


# Dialogs only a person clears; a headless core shown one stays there, queued work or not.
# Approvals and pickers are absent: the core's own turn raises and answers those.
PERSON_ONLY_GATES = ("folder-trust", "bypass-permissions", "login")


def waits_for_a_person(verdict: Verdict) -> bool:
    """True when the pane is held by a dialog that no automation will answer."""
    return verdict.state == "busy" and verdict.reason in PERSON_ONLY_GATES


def blocks_dispatch(verdict: Verdict, adapter: RuntimeAdapter) -> bool:
    """True when this pane holds every queued task: an abnormal frame on any runtime, or a
    draft on a runtime whose dispatcher waits for idle-ready."""
    return verdict.state == "abnormal" or (verdict.state == "pending" and adapter.pending_blocks_dispatch)


# A working turn queues typed input (the Claude notifier delivers like the Monitor
# tool); a dialog, a parked banner or an unreadable pane does not.
def accepts_input(verdict: Verdict) -> bool:
    """True when a line typed now reaches the composer: idle, a draft, or a running turn."""
    if verdict.state in ("idle-ready", "pending"):
        return True
    return verdict.state == "busy" and verdict.reason == "working"


def _turns(capture: str) -> List[Tuple[str, bool]]:
    """(key, ran) per completed turn on screen, oldest first. `ran`: longer than 1s and no
    refusal line in its output. Keyed by `✻ … done <time>` so a changing suffix is one turn."""
    turns, refused = [], False
    for ln in (capture or "").splitlines():
        plain = _SGR.sub("", ln)
        m = TURN_DONE.match(plain)
        if not m:
            # A prompt row starts the next turn; a refusal above it was the last one's.
            refused = (refused and not plain.lstrip().startswith("❯")) or refusal_line(plain.strip())
            continue
        ran = bool(m.group("dur")) and not TURN_SHORT.fullmatch(m.group("dur")) and not refused
        turns.append((re.sub(r"(\bdone\s+\S+(?:\s*[AP]M)?).*", r"\1", m.group(0).strip()), ran))
        refused = False
    return turns


def finished_turns(capture: str) -> frozenset:
    """The completed turns on screen that ran (see `_turns`)."""
    return frozenset(key for key, ran in _turns(capture) if ran)


def _below_composer(capture: str) -> List[str]:
    """The rows under the last prompt row: where the CLI draws its status banners."""
    lines = (capture or "").splitlines()
    last = max((i for i, ln in enumerate(lines) if _SGR.sub("", ln).lstrip().startswith("❯")),
               default=len(lines) - 1)
    return lines[last + 1:]


# What still moves on a waiting pane's finished turns: a status glyph the CLI cycles and a
# bare count ("1 monitor still running"). cli_wedge keeps counts for its frozen-turn test.
_STATUS_GLYPH = re.compile(r"^(\s*)[✻✶✳✢✽✺✹✷✸✦✧∗]\s+", re.MULTILINE)
_COUNT = re.compile(r"\d+")


def progress_id(capture: str) -> str:
    """Identity of the work on screen: the rows above the composer, with clocks, durations,
    spinners, status glyphs and bare counts normalised away. Status rows under it (footer
    counters, a limit banner) are not work. Compared across ticks of a waiting pane, whose
    rows above the composer are finished turns: text that advances is progress, a counter
    that ticks is not."""
    lines = (capture or "").splitlines()
    above = normalize("\n".join(lines[:len(lines) - len(_below_composer(capture))]))
    return state_id(_COUNT.sub("#", _STATUS_GLYPH.sub(r"\1<glyph> ", above)))


def _limit_row(ln: str) -> bool:
    plain = _SGR.sub("", ln)
    abn = frame_abnormal(plain)
    return bool(LIMIT_WAIT.search(plain)) or (
        abn is not None and all(n in PROVIDER_LIMIT_PATTERNS for n in abn.names))


def limit_banner(capture: str) -> Optional[FrameAbnormal]:
    """The provider-limit verdict for the banner under the composer, else None. The CLI's
    "Usage limit reached" banner is not one of cli_wedge's whole-line banners, so its own
    signature vouches for the family here only; dispatch verdicts are unchanged."""
    below = "\n".join(_below_composer(capture))
    abn = frame_abnormal(below)
    if abn is not None:
        return abn if abn.kind == "provider-limit" else None
    return FrameAbnormal("provider-limit", ("quota-limit",), False) if LIMIT_WAIT.search(below) else None


@dataclass(frozen=True)
class LimitBaseline:
    banner: Tuple[str, ...]   # the banner rows, reset time included
    seen: frozenset           # every completed turn on screen when that banner was first seen


def limit_baseline(capture: str) -> LimitBaseline:
    return LimitBaseline(tuple(_SGR.sub("", ln).strip() for ln in _below_composer(capture)
                               if _limit_row(ln)),
                         frozenset(key for key, _ in _turns(capture)))


def strip_limit_banners(capture: str) -> str:
    """`capture` without the limit banner under its composer, for a caller that has judged
    it stale. Rows inside turns (a refusal) are evidence and stay."""
    lines = capture.splitlines()
    first_below = len(lines) - len(_below_composer(capture))
    return "\n".join(ln for i, ln in enumerate(lines) if i < first_below or not _limit_row(ln))


def limit_is_stale(abn, workspace, socket, session, probe=False, capture=None, baseline=None) -> bool:
    """A quota-only banner is a line from before the reset, scrollback, not a hold, when
    the seat shows it is not held: the newest turn finished since `baseline` (limit_baseline
    of the first capture showing the banner) ran, or a FRESH proxy record says accepted for a
    seat ROUTED through that proxy. A newest turn that was refused holds whatever the record
    says. Anything else keeps the hold -- another abnormal family, no newer turn, an
    unrouted or unobserved seat, or a record absent, stale or rejected. A retry never
    reaches here: classify_pane returns on it before any gate."""
    if abn is None or abn.kind != "provider-limit":
        return False
    if any(n not in PROVIDER_LIMIT_PATTERNS for n in abn.names):
        return False
    if capture is not None and baseline is not None:
        new = [ran for key, ran in _turns(capture) if key not in baseline.seen]
        if new:
            return new[-1]
    try:
        ws = workspace if workspace is not None else resolve_workspace()
        return provider_allows_now(ws, socket, session, probe=probe)
    except Exception:  # noqa: BLE001 -- an unreadable record is not permission
        return False


def limit_step(pane, baseline, workspace=None, socket=None, session=None, owes_work=True):
    """(stale, baseline) for this poll's limit banner. The baseline is taken when a banner
    first shows, and again when its text changes; the newest turn since then decides.
    A banner holds nothing on a seat that owes no work (`owes_work` False). An unknown
    queue (None) holds like an owed one: a count that could not be read is not zero.
    The seat monitor and the pool sweep both judge staleness here."""
    abn = limit_banner(pane or "")
    if abn is None:
        return False, None
    current = limit_baseline(pane)
    if baseline is None or baseline.banner != current.banner:
        baseline = current
    if owes_work is False:
        return True, baseline
    return limit_is_stale(abn, workspace, socket, session, capture=pane, baseline=baseline), baseline


def classify_pane(capture: Optional[str], adapter: RuntimeAdapter, workspace=None,
                  socket=None, session=None, probe=False) -> Verdict:
    """One verdict for a captured pane. A failed or empty capture is UNKNOWN, never idle.
    `workspace` locates the proxy's quota record (None: the configured one); `socket`
    and `session` name the seat whose routing that record must vouch for."""
    if capture is None:
        return Verdict("unknown", "no capture")
    if not capture.strip():
        return Verdict("unknown", "empty pane")
    lines = _tail_lines(capture)
    tail = "\n".join(_SGR.sub("", ln) for ln in lines)
    # cli_wedge ranks abnormal text above motion. A retry keeps that rank against
    # a dialog too; a parked line may be the dialog's own text, so the gate reads first.
    abn = frame_abnormal(tail)
    if abn and abn.retrying:
        return Verdict("abnormal", ",".join(abn.names))
    # FULL capture, not the TAIL_LINES tail: a wrapped draft past that window
    # loses its own glyph line to truncation and the footer reads as empty.
    line = prompt_line(capture, adapter)
    # _gate's after_prompt() must see the same capture line was found in, or
    # it computes "below" from a view that never contained that line at all.
    gate = _gate(capture, "\n".join(_SGR.sub("", ln) for ln in live_tail(lines)), line, adapter)
    if gate:
        return Verdict("busy", gate)
    if abn and not limit_is_stale(abn, workspace, socket, session, probe):
        return Verdict("abnormal", ",".join(abn.names))
    if frame_working(tail):
        return Verdict("busy", "working")
    if line is not None and line.text:
        return Verdict("pending", "text at the prompt", line.text)
    if ((adapter.idle_ready.search(tail) and
         (not adapter.idle_requires_prompt or line is not None))
            or (line is not None and line.placeholder)):
        return Verdict("idle-ready", "idle footer or empty composer", "" if line else None)
    return Verdict("unknown", "no idle affordance")


def observe(socket_path: str, session: str, adapter: RuntimeAdapter, tmux_bin: str = "tmux",
            runner: Callable = subprocess.run, env: Optional[dict] = None, workspace=None,
            probe=False) -> Verdict:
    """Capture the core window through cli_wedge's one capture path, then classify it."""
    target = core_target(socket_path, session, tmux_bin, runner, env)
    if target is None:
        return Verdict("unknown", "no core window")
    # Both runtimes need attributes: Codex draws a dim placeholder, Claude a
    # grey ghost suggestion, and prompt_line() strips either before deciding pending.
    text = capture_pane(socket_path, target, tmux_bin, runner, env, escapes=True)
    return classify_pane(text, adapter, workspace, socket_path, session, probe)


# tmux-send-line.sh's contract; 2 is its own argument rejection.
EXIT_STATUS = {0: "sent", 2: "rejected", 3: "no-session", 4: "no-tmux", 5: "pending", 6: "queued", 7: "unknown"}


def deliver(line: str, session: str, runtime: str, socket_path: Optional[str] = None,
            refuse_if_pending: bool = False, skip_if_queued: Optional[str] = None, dry_run: bool = False,
            runner: Callable = subprocess.run, script: Path = SEND_LINE) -> Outcome:
    """Type one line + Enter into the core pane via the ONE sender, scripts/tmux-send-line.sh.
    The prompt read, the queued-input policy and the lock are its; this only maps its exit code."""
    if runtime not in ADAPTERS:
        raise ValueError(f"unknown runtime {runtime!r} (expected one of {sorted(ADAPTERS)})")
    argv = ["bash", str(script), session, line, "--runtime", runtime]
    if socket_path:
        argv += ["--socket", socket_path]
    if refuse_if_pending:
        argv.append("--refuse-if-pending")
    if skip_if_queued:
        argv += ["--skip-if-queued", skip_if_queued]
    if dry_run:
        argv.append("--dry-run")
    proc = runner(argv, capture_output=True, text=True, timeout=60)
    code = int(getattr(proc, "returncode", 1))
    message = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or "")).strip()
    return Outcome(EXIT_STATUS.get(code, "failed"), code, message)


def _read_stdin() -> str:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return sys.stdin.read()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pane_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("safe-states")  # no --runtime: the state vocabulary is runtime-independent
    for name in ("classify", "pending", "after", "safe"):
        p = sub.add_parser(name)
        p.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
        p.add_argument("--width", type=int, default=0)
        if name == "classify":
            p.add_argument("--json", action="store_true")
        if name in ("classify", "safe"):
            p.add_argument("--workspace", default=None, help="where state/quota-state.json lives")
            p.add_argument("--socket", default=os.environ.get("SUTANDO_TMUX_SOCKET"))
            p.add_argument("--session", default=None, help="the seat the record must vouch for")
            p.add_argument("--probe", action="store_true", help="refresh a stale record through the proxy first")
    lo = sub.add_parser("leftover")
    lo.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    lo.add_argument("--workspace", default=None, help="where state/quota-state.json lives")
    lo.add_argument("--socket", default=os.environ.get("SUTANDO_TMUX_SOCKET"))
    lo.add_argument("--session", default=None, help="the seat the record must vouch for")
    ct = sub.add_parser("composer-text")
    ct.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    cf = sub.add_parser("composer-frame")
    cf.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    hp = sub.add_parser("healthy")
    hp.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    hp.add_argument("--workspace", default=None, help="where state/quota-state.json lives")
    hp.add_argument("--socket", default=os.environ.get("SUTANDO_TMUX_SOCKET"))
    hp.add_argument("--session", default=None, help="the seat the record must vouch for")
    hp.add_argument("--probe", action="store_true", help="refresh a stale record through the proxy first")
    d = sub.add_parser("deliver")
    d.add_argument("session")
    d.add_argument("line")
    d.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    d.add_argument("--socket", default=os.environ.get("SUTANDO_TMUX_SOCKET"))
    d.add_argument("--refuse-if-pending", action="store_true")
    d.add_argument("--skip-if-queued", default=None)
    d.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "safe-states":
        # Emitting the SAFE set keeps a caller's membership test fail-closed: anything
        # absent -- a new state, a typo, an empty read -- refuses without being listed.
        print(" ".join(s for s in STATES if not state_is_unsafe(s)))
        return 0
    adapter = ADAPTERS[a.runtime]
    if a.cmd == "classify":
        v = classify_pane(_read_stdin(), adapter, getattr(a, "workspace", None),
                          getattr(a, "socket", None), getattr(a, "session", None),
                          getattr(a, "probe", False))
        print(json.dumps(v.as_dict()) if a.json else v.state)
        return 0
    if a.cmd == "pending":
        # None (no prompt line) and "" (empty composer) are opposite answers; printing
        # both as "" let a caller's `[ -n "$PENDING" ]` authorize a send. Unknown exits 3.
        text = pending_text(_read_stdin(), adapter, getattr(a, "width", 0))
        if text is None:
            print("pane_gate: no prompt line found — prompt unknown", file=sys.stderr)
            return EXIT_UNSAFE
        print(text)
        return 0
    if a.cmd == "healthy":
        # The notifier's pre-typing question, answered by the same verdict every
        # caller gets: exit 0 when typed input reaches the composer, else refuse.
        v = classify_pane(_read_stdin(), adapter, getattr(a, "workspace", None),
                          getattr(a, "socket", None), getattr(a, "session", None),
                          getattr(a, "probe", False))
        if not accepts_input(v):
            print(f"pane_gate: {v.state} ({v.reason}) — not accepting input", file=sys.stderr)
            return EXIT_UNSAFE
        print(v.state)
        return 0
    if a.cmd == "leftover":
        # Exit 0 + the text only when the composer holds an automation leftover and nothing
        # else; any other content, or no prompt, exits 1 and must be left alone.
        text = automation_leftover(_read_stdin(), adapter, a.workspace, a.socket, a.session)
        if text is None:
            return 1
        print(text)
        return 0
    if a.cmd == "composer-text":
        # Same None/"" contract as "pending", via the frame-stripping parser
        # Claude's indented word-wrap needs instead of a terminal-wrap width.
        text = composer_text(_read_stdin(), adapter)
        if text is None:
            print("pane_gate: no prompt line found — prompt unknown", file=sys.stderr)
            return EXIT_UNSAFE
        print(text)
        return 0
    if a.cmd == "composer-frame":
        seen = composer_frame_visible(_read_stdin(), adapter)
        if seen is None:
            print("pane_gate: no prompt line found — frame unknown", file=sys.stderr)
            return EXIT_UNSAFE
        print("visible" if seen else "cut")
        return 0
    if a.cmd == "safe":
        v = classify_pane(_read_stdin(), adapter, getattr(a, "workspace", None),
                          getattr(a, "socket", None), getattr(a, "session", None),
                          getattr(a, "probe", False))
        if state_is_unsafe(v.state):
            print(f"pane_gate: {v.state} — not safe to type into", file=sys.stderr)
            return EXIT_UNSAFE
        print(v.state)
        return 0
    if a.cmd == "after":
        print(after_prompt(_read_stdin(), adapter, getattr(a, "width", 0)))
        return 0
    out = deliver(a.line, a.session, a.runtime, a.socket, a.refuse_if_pending, a.skip_if_queued, a.dry_run)
    print(f"{out.status}: {out.message}" if out.message else out.status, file=sys.stderr if out.code else sys.stdout)
    return out.code


if __name__ == "__main__":
    sys.exit(main())
