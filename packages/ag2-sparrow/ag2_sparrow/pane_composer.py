#!/usr/bin/env python3
"""The one reader of a CLI's input box (its composer) from a captured tmux pane: the prompt row, the text typed in it, and whether the box's frame is on screen.

Pure text in, answers out; nothing here captures a pane or sends a key. Two callers
share it so they cannot disagree on what is typed text:

    src/delivery/pane_gate.py     the core's gate; it re-exports every name below
    src/claude_model_switch.py    typing /model, for the model-switch skill and for
                                  ag2-sparrow's claude-local adapter (bundled verbatim)

A runtime is DATA: any object with `glyph`, `alternate_glyphs`, `idle_ready` and
`composer_requires_single_line` (pane_gate's RuntimeAdapter, or a Composer here).

    prompt_line / pending_text   the LAST prompt row only (styled capture: hints dropped)
    composer_text                the whole box, dewrapped: '' empty, None no prompt row
    composer_frame_visible       the frame below the box is on screen
    composer_boxed               a rule sits right above the prompt row and one below it
    frame_plain                  a styled capture as composer_text reads one

Imports nothing outside the standard library: the package bundles this file alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

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

# Claude Code's idle footer; pane_gate and core-input-watch.py read it from here.
CLAUDE_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|←\s*for agents\b", re.I)


@dataclass(frozen=True)
class Composer:
    """What reading one runtime's input box needs, and no more."""
    glyph: str
    idle_ready: "re.Pattern[str]"
    alternate_glyphs: Tuple[str, ...] = ()
    composer_requires_single_line: bool = False


CLAUDE_COMPOSER = Composer(glyph="❯", idle_ready=CLAUDE_IDLE)


@dataclass(frozen=True)
class PromptLine:
    text: str
    placeholder: bool


def _prompt_glyph(raw: str, adapter, allow_indent: bool = True) -> Optional[str]:
    plain = _SGR.sub("", raw)
    if allow_indent:
        plain = plain.lstrip(" \t")
    return next((glyph for glyph in (adapter.glyph, *adapter.alternate_glyphs)
                 if plain.startswith(glyph)), None)


def prompt_line(capture: str, adapter, width: int = 0) -> Optional[PromptLine]:
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


def pending_text(capture: str, adapter, width: int = 0) -> Optional[str]:
    """Text typed at the current prompt; "" when the composer is empty; None when no prompt is seen."""
    line = prompt_line(capture, adapter, width)
    return None if line is None else line.text


def after_prompt(capture: str, adapter, width: int = 0) -> str:
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


def composer_text(capture: str, adapter=CLAUDE_COMPOSER) -> Optional[str]:
    """The composer's full typed content, dewrapped, or None with no <glyph> line
    at all; "" for an empty composer. The ONE parser for task-notifier.sh's
    EXACT-equality staging checks -- core-input-watch.py's `_composer_text`
    aliases this rather than re-deriving it.

    Single-line adapters require a visible footer and preserve all typed bytes.
    From the bottommost prompt line to the end of the capture. Below an editable
    composer the CLI renders its own frame -- box rule(s), at most one idle-footer
    row, at most one hint/tip row -- so the strip removes exactly those, once each,
    from the back. It never re-classifies an interior row: an owner's own typed
    line that happens to read one of those rows' words survives, because only the
    LAST matching row of each kind is ever popped, and never a second time. The cut
    at the closing rule follows the same rule: the LAST rule row, never an interior one.
    """
    if adapter.composer_requires_single_line:
        rows = capture.split("\n")
        start = next((i for i in range(len(rows) - 1, -1, -1)
                      if _prompt_glyph(rows[i], adapter, allow_indent=False) is not None), None)
        footer = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].strip()), None)
        if (start is None or footer is None or footer <= start
                or not adapter.idle_ready.search(_SGR.sub("", rows[footer]))):
            return None
        block = rows[start:footer]
        if len(block) > 1 and block[-1] == "":
            block.pop()
        if len(block) != 1:
            return None
        glyph = _prompt_glyph(block[0], adapter)
        raw = block[0][block[0].find(glyph) + len(glyph):]
        ghostless, ghosts = _GHOST.subn("", raw)
        if ghosts and _SGR.sub("", ghostless) in ("", " ", "\u00a0"):
            return ""
        while _SGR.match(raw):
            raw = raw[_SGR.match(raw).end():]
        text = raw[1:] if raw[:1] in (" ", "\u00a0") else raw
        return None if re.search(r"[\x00-\x1f\x7f]", text) else text
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


def composer_frame_visible(capture: str, adapter=CLAUDE_COMPOSER) -> Optional[bool]:
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


def frame_plain(capture: Optional[str], adapter=CLAUDE_COMPOSER) -> str:
    """A styled capture (`capture-pane -e`) as composer_text reads one: styling kept on prompt
    rows, where it tells a hint from typed text, and dropped from every other row, where a
    coloured rule or footer would otherwise read as typed text."""
    return "\n".join(raw if _prompt_glyph(raw, adapter) is not None else _SGR.sub("", raw)
                     for raw in (capture or "").splitlines())


def composer_boxed(capture: Optional[str], adapter=CLAUDE_COMPOSER) -> bool:
    """The last prompt row sits inside its frame: a rule on the row right above it and a rule
    somewhere below. composer_text alone reads a bare prompt row as an empty box; a caller
    that types on "empty" asks this too, so a box scrolled or cut out of its frame, or a
    dialog's caret row, is never typed into."""
    lines = [ln for ln in frame_plain(capture, adapter).splitlines() if ln.strip()]
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if _prompt_glyph(lines[i], adapter) is not None), None)
    if start is None or start == 0 or not BORDER_LINE.match(lines[start - 1]):
        return False
    return any(BORDER_LINE.match(row) for row in lines[start + 1:])


_NUMBERED_ROW = re.compile(r"^\s*(?:❯\s*)?\d+\.\s")
_CARET_ROW = re.compile(r"^\s*❯\s*\d+\.\s")
# The CLI limit menu's own rows: its first option, and one of the others it always offers with it.
LIMIT_MENU_STOP = re.compile(r"^\s*(?:❯\s*)?1\.\s+Stop and wait for (?:the |your )?(?:session |usage |weekly )?"
                             r"limit to reset\s*$", re.I)
LIMIT_MENU_MORE = re.compile(r"^\s*(?:❯\s*)?\d\.\s+(?:(?:Switch to|Add funds to continue with) (?:extra usage|usage "
                             r"credits)|Wait here, then continue automatically\b)", re.I)


def limit_menu(text: str) -> bool:
    """Whether Claude Code's own usage-limit menu is the live dialog on a plain capture: the last run of
    numbered rows has the caret on one, the menu's first option and one of the others it always offers
    with it. Words that only read like the menu (a reply, a quoted message) have no such run."""
    lines = (text or "").splitlines()
    end = max((i for i, ln in enumerate(lines) if _NUMBERED_ROW.match(ln)), default=None)
    if end is None:
        return False
    start = end
    while start > 0 and _NUMBERED_ROW.match(lines[start - 1]):
        start -= 1
    menu = lines[start:end + 1]
    return (any(_CARET_ROW.match(ln) for ln in menu) and any(LIMIT_MENU_STOP.match(ln) for ln in menu)
            and any(LIMIT_MENU_MORE.match(ln) for ln in menu))
