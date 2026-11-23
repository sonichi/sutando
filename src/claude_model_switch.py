#!/usr/bin/env python3
"""What switching Claude Code's model by typing `/model` relies on: which model names may be typed,
the two texts Claude Code answers with, and whether its input box is empty (read by the core's
own parser, src/pane_composer.py).

One source for the core's model-switch skill (its shell scripts ask this file for the patterns)
and for ag2-sparrow's claude-local adapter (bundled verbatim; the package drift test pins the copy).

    python3 claude_model_switch.py model-pattern        # the names that may be typed, as an ERE
    python3 claude_model_switch.py accept <model>       # Claude Code's line for a switch to it, as an ERE
    python3 claude_model_switch.py count <model>        # how many such lines the screen on stdin holds
    python3 claude_model_switch.py count <model> --settings FILE   # the same, a [1m] line without its size
                                                                   # counting once FILE's model names it
    python3 claude_model_switch.py confirm-text         # the text of Claude Code's confirm dialog
"""
from __future__ import annotations

import json
import re
import sys

try:
    from . import pane_composer       # the vendored package layout
except ImportError:                   # a flat src/ load: the skill's scripts, the tests
    import pane_composer

# Aliases Claude Code's /model takes, or a full id; a family alias or an id may carry a context tag.
MODEL_PATTERN = r"^(default|(opus|sonnet|haiku|fable)(\[1m\])?|claude-[a-z0-9.-]+(\[1m\])?)$"
MODEL_RE = re.compile(MODEL_PATTERN)
MODEL_SET, MODEL_CONFIRM = "Set model to", "Yes, switch"


def composer_text(shown) -> "str | None":
    """What sits unsent in Claude Code's input box, from a styled or plain capture: '' when it is
    empty, None when the screen shows no prompt row. The core's own reading (src/pane_composer.py,
    which pane_gate re-exports), not a second parser: every row of the box counts, so a draft of
    several lines is text even when its first line is blank, as is a dialog's caret row."""
    return pane_composer.composer_text(pane_composer.frame_plain(shown), pane_composer.CLAUDE_COMPOSER)


def composer_boxed(shown) -> bool:
    """The prompt row sits inside its frame (a rule right above it, a rule below). An empty reading
    of a prompt with no frame around it is not evidence the box is empty: ask this before typing."""
    return pane_composer.composer_boxed(shown, pane_composer.CLAUDE_COMPOSER)


def composer_typed(shown) -> "str | None":
    """What is TYPED on the prompt row of a styled capture: a hint Claude Code draws there is styled
    and dropped, so words that only read like a hint (`Try "fix lint errors"`, typed by the owner)
    are kept as text. '' with nothing typed there, None with no prompt row. composer_text decides a
    hint by its wording; a caller about to type asks this too."""
    return pane_composer.pending_text(shown or "", pane_composer.CLAUDE_COMPOSER)


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


def accepted_count(model: str, shown) -> int:
    """How many of Claude Code's lines on screen are a switch to exactly `model`: its family, its
    version and its context variant. A line for the ordinary model is not one for its [1m] variant, nor
    the other way round; a [1m] switch whose line does not say so stays unconfirmed. `default` names
    no variant (the default may itself be a 1M model), so its line counts either way."""
    wants_1m, pattern = model.endswith("[1m]"), model_accepted(model)
    return sum(1 for line in (shown or "").splitlines()
               if pattern.search(line) and (model == "default" or bool(re.search(r"1m", line, re.I)) == wants_1m))


def family_count(model: str, shown) -> int:
    """How many lines on screen name a switch to `model`'s family and version, whatever context size they
    state. Claude Code 2.1.322 answers `/model sonnet[1m]` with "Set model to Sonnet 5.5 and saved as your
    default for new sessions", naming no context size (measured 2026-11-22; its saved setting was
    `sonnet[1m]`): such a line is only half the evidence for a [1m] switch, the saved setting the rest."""
    pattern = model_accepted(model)
    return sum(1 for line in (shown or "").splitlines() if pattern.search(line))


def saved_setting(settings_file) -> str:
    """The `model` a CLI's settings.json holds, '' when it cannot be read or names none."""
    try:
        with open(settings_file, encoding="utf-8") as f:
            saved = json.load(f).get("model")
    except (OSError, ValueError, AttributeError, TypeError):
        return ""
    return saved if isinstance(saved, str) else ""


def settled_count(model: str, shown, saved: str = "") -> int:
    """`accepted_count`, except that a [1m] switch the CLI answered without naming the context size counts
    once its saved setting (`saved`) names exactly that model: then every line of its family does. Taken
    before a send and again after, with the setting as it is each time, a NEW line is still required."""
    if model.endswith("[1m]") and saved == model:
        return family_count(model, shown)
    return accepted_count(model, shown)


def main(argv) -> int:
    if argv[:1] == ["model-pattern"] and len(argv) == 1:
        print(MODEL_PATTERN)
        return 0
    if argv[:1] == ["accept"] and len(argv) == 2 and MODEL_RE.match(argv[1]):
        print(accept_pattern(argv[1]))
        return 0
    if argv == ["confirm-text"]:
        print(MODEL_CONFIRM)
        return 0
    if argv[:1] == ["count"] and len(argv) == 2 and MODEL_RE.match(argv[1]):
        print(accepted_count(argv[1], sys.stdin.read()))
        return 0
    if argv[:1] == ["count"] and len(argv) == 4 and argv[2] == "--settings" and MODEL_RE.match(argv[1]):
        print(settled_count(argv[1], sys.stdin.read(), saved_setting(argv[3])))
        return 0
    print("usage: claude_model_switch.py model-pattern | accept <model> | count <model> [--settings FILE] | "
          "confirm-text", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
