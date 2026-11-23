#!/usr/bin/env python3
"""What switching Claude Code's model by typing `/model` relies on: which model names may be typed,
the two texts Claude Code answers with, and whether its input box is empty (read by the core's
own parser, src/pane_composer.py).

One source for the core's model-switch skill (its shell scripts ask this file for the patterns)
and for ag2-sparrow's claude-local adapter (bundled verbatim; the package drift test pins the copy).

    python3 claude_model_switch.py model-pattern        # the names that may be typed, as an ERE
    python3 claude_model_switch.py accept <model>       # Claude Code's line for a switch to it, as an ERE
    python3 claude_model_switch.py count <model>        # how many such lines the screen on stdin holds
    python3 claude_model_switch.py baseline <model> --settings FILE           # pre-send evidence + FILE's mark
    python3 claude_model_switch.py settled <model> --settings FILE --baseline B  # 1 when the screen shows
                                                       # a NEW switch line (a size-omitted [1m] one only after a fresh save)
    python3 claude_model_switch.py confirm-text         # the text of Claude Code's confirm dialog
"""
from __future__ import annotations

import hashlib
import json
import os
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


# A line that states a context size at all ("1M context", "200k"): only a line that states none is size-omitted.
STATES_SIZE = re.compile(r"\d+(\.\d+)?\s*[km]\b|\bcontext\b", re.I)


def evidence(model: str, shown) -> "tuple[int, int]":
    """(strict, omitted) lines on screen for a switch to `model`: strict ones are `accepted_count`'s;
    omitted ones name its family and version but state no context size. Claude Code 2.1.322 answers
    `/model sonnet[1m]` with "Set model to Sonnet 5.5 and saved as your default for new sessions"
    (measured 2026-11-22). A line stating another size contradicts a [1m] switch and is in neither."""
    pattern = model_accepted(model)
    omitted = sum(1 for line in (shown or "").splitlines() if pattern.search(line) and not STATES_SIZE.search(line))
    return accepted_count(model, shown), omitted


def saved_setting(settings_file) -> str:
    """The `model` a CLI's settings.json holds, '' when it cannot be read or names none."""
    try:
        with open(settings_file, encoding="utf-8") as f:
            saved = json.load(f).get("model")
    except (OSError, ValueError, AttributeError, TypeError):
        return ""
    return saved if isinstance(saved, str) else ""


def settings_mark(settings_file) -> str:
    """An opaque token for the settings file as it is now (mtime, size, saved model): a save changes it."""
    try:
        st = os.stat(settings_file)
        stamp = "%d.%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = "absent"
    return "%s.%s" % (stamp, hashlib.sha256(saved_setting(settings_file).encode()).hexdigest()[:12])


def baseline(model: str, shown, settings_file) -> str:
    """The pre-send reading `settled` compares against: "<strict> <omitted> <settings mark>"."""
    strict, omitted = evidence(model, shown)
    return "%d %d %s" % (strict, omitted, settings_mark(settings_file))


def settled(model: str, shown, settings_file, base: str) -> bool:
    """A NEW strict line accepts any switch. A [1m] switch is also accepted by a NEW size-omitted line,
    but only when the settings file was saved since the baseline and now names exactly `model`: an
    unchanged pane is never a switch, whatever the setting says, and an old setting confirms nothing."""
    try:
        b_strict, b_omitted, b_mark = base.split(" ")
        b_strict, b_omitted = int(b_strict), int(b_omitted)
    except ValueError:
        return False
    strict, omitted = evidence(model, shown)
    if strict > b_strict:
        return True
    return (model.endswith("[1m]") and omitted > b_omitted and settings_mark(settings_file) != b_mark
            and saved_setting(settings_file) == model)


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
    if argv[:1] == ["baseline"] and len(argv) == 4 and argv[2] == "--settings" and MODEL_RE.match(argv[1]):
        print(baseline(argv[1], sys.stdin.read(), argv[3]))
        return 0
    if (argv[:1] == ["settled"] and len(argv) == 6 and argv[2] == "--settings" and argv[4] == "--baseline"
            and MODEL_RE.match(argv[1])):
        print("1" if settled(argv[1], sys.stdin.read(), argv[3], argv[5]) else "0")
        return 0
    print("usage: claude_model_switch.py model-pattern | accept <model> | count <model> | "
          "baseline <model> --settings FILE | settled <model> --settings FILE --baseline B | confirm-text", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
