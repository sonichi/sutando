#!/usr/bin/env python3
"""What switching Claude Code's model by typing `/model` relies on: which model names may be typed,
the two texts Claude Code answers with, and whether its input box is empty.

One source for the core's model-switch skill (its shell scripts ask this file for the patterns)
and for ag2-sparrow's claude-local adapter (bundled verbatim; the package drift test pins the copy).

    python3 claude_model_switch.py model-pattern        # the names that may be typed, as an ERE
    python3 claude_model_switch.py accept <model>       # Claude Code's line for a switch to it, as an ERE
"""
from __future__ import annotations

import re
import sys

# Aliases Claude Code's /model takes, or a full id; a family alias or an id may carry a context tag.
MODEL_PATTERN = r"^(default|(opus|sonnet|haiku|fable)(\[1m\])?|claude-[a-z0-9.-]+(\[1m\])?)$"
MODEL_RE = re.compile(MODEL_PATTERN)
MODEL_SET, MODEL_CONFIRM = "Set model to", "Yes, switch"
PROMPT_GLYPH = "❯"
# Styling, and the dim or grey runs that are a placeholder or a suggestion, never text someone typed
# (the same two patterns as src/delivery/pane_gate.py).
_SGR = re.compile(r"\x1b\[[0-9:;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_GHOST = re.compile(r"\x1b\[(?:2|38;5;(?:23[2-9]|24[0-9]|25[0-5]))m.*?(?=\x1b\[(?:0|22|39)m|$)")


def composer_text(shown) -> "str | None":
    """What sits unsent in Claude Code's input box, from a styled capture: the text after the last
    prompt glyph, '' when the box is empty, None when no prompt is on screen (unknown, never empty).
    An open menu's rows start with the glyph too, so a menu left open reads as text, not as empty."""
    found = None
    for raw in (shown or "").splitlines():
        if _SGR.sub("", raw).lstrip(" \t").startswith(PROMPT_GLYPH):
            found = _SGR.sub("", _GHOST.sub("", raw[raw.find(PROMPT_GLYPH) + 1:])).strip(" \t ")
    return found


def accept_pattern(model: str) -> str:
    """Claude Code's line for a switch to `model`, as an ERE to match ignoring case. It echoes display
    names ("Set model to Opus 4.6"): an alias accepts any version of its family at a boundary, an id
    exactly its own version ("Opus 5" is not "Opus 5.1")."""
    plain = re.sub(r"\[1m\]$", "", model)
    family = re.sub(r"^claude-([a-z]+)-?.*", r"\1", plain)
    version = re.match(r"^claude-[a-z]+-([0-9]+(-[0-9]+)*)", plain)
    if version:
        return r"%s %s %s([^0-9.]|$)" % (MODEL_SET, family, version.group(1).replace("-", r"\."))
    return r"%s %s( [0-9][0-9.]*)?([^0-9.a-z]|$)" % (MODEL_SET, re.escape(family))


def model_accepted(model: str) -> "re.Pattern":
    return re.compile(accept_pattern(model), re.I | re.M)


def main(argv) -> int:
    if argv[:1] == ["model-pattern"] and len(argv) == 1:
        print(MODEL_PATTERN)
        return 0
    if argv[:1] == ["accept"] and len(argv) == 2 and MODEL_RE.match(argv[1]):
        print(accept_pattern(argv[1]))
        return 0
    print("usage: claude_model_switch.py model-pattern | accept <model>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
