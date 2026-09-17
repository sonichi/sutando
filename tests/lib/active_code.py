#!/usr/bin/env python3
"""The one active-code/comment predicate these guards share.

Four private copies drifted apart; a guard that reads its own comment rule wrong
passes on a commented-out caller. Quote state matters: a caller named inside a
quoted string is text, not an invocation.
"""

_SEPS = " \t;&|(){}"


def _strip_comment(line: str) -> str:
    """Drop a `#` comment, honouring quotes and token boundaries.

    `#` opens a comment only outside quotes and at the start of a token, so
    `a#b` and `"# not a comment"` survive while `:;# x` and `run  # x` do not.
    """
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "#" and (i == 0 or line[i - 1] in _SEPS):
            return line[:i]
    return line


def quoted_spans(line: str) -> list[tuple[int, int]]:
    """(start, end) of each quoted run, so callers can ignore quoted text."""
    spans, quote, start = [], None, 0
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                spans.append((start, i + 1))
                quote = None
        elif ch in "'\"":
            quote, start = ch, i
    if quote:
        spans.append((start, len(line)))
    return spans


def unquoted(line: str) -> str:
    """The line with quoted runs blanked out. `echo 'python3 x.py'` names nothing."""
    spans = quoted_spans(line)
    if not spans:
        return line
    out = list(line)
    for a, b in spans:
        for i in range(a, b):
            out[i] = " "
    return "".join(out)


def active_lines(text: str) -> list[str]:
    """Each line's code part. Comment-only lines are dropped entirely."""
    out = []
    for ln in text.splitlines():
        code = _strip_comment(ln)
        if code.strip():
            out.append(code)
    return out


def active_text(text: str) -> str:
    return "\n".join(active_lines(text))


import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_tokens(seg: str) -> list[str]:
    """`seg`'s tokens with a leading env/VAR= prefix peeled off.

    `FOO=1 cmd` runs `cmd`; `BAD-NAME=1 cmd` is not a valid assignment (a
    hyphen can't start a shell identifier), so bash tries to RUN it and
    fails — peeling it here would wrongly credit `cmd` as invoked."""
    import shlex
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    changed = True
    while changed and toks:
        changed = False
        if _IDENT_RE.match(toks[0]):
            toks = toks[1:]; changed = True
        elif toks[0] == "env" and len(toks) > 1:
            toks = toks[1:]; changed = True
        elif toks[0] == "timeout" and len(toks) > 1:
            toks = toks[1:]; changed = True
            while toks and (toks[0].startswith("-") or toks[0].isdigit()):
                drop = 2 if toks[0] in ("-k", "-s", "--kill-after", "--signal") else 1
                toks = toks[drop:]
    return toks


# python3's real option grammar, verified by EXECUTION on both this host's
# python3 AND the repo's explicit 3.9 floor (python39-compat.yml) -- unlisted tokens fail CLOSED.
_TERMINAL_LONG = frozenset(("--help", "--version", "--help-env", "--help-xoptions", "--help-all"))
_VALUE_LONG = frozenset(("--check-hash-based-pycs",))
_HASH_PYCS_VALUES = frozenset(("always", "default", "never"))
_TERMINAL_CHARS = frozenset("hV?")
_SCRIPT_CHARS = frozenset("cm")
_VALUE_CHARS = frozenset("WX")
_VALUELESS_CHARS = frozenset("bBdEiIOqRsStuvx")


def _option_kind(tok: str):
    """Classify one option token by the real grammar above, short clusters
    scanned left to right so ownership (not a fixed position) decides
    attached vs. separate for -W/-X (measured: `-uWX`/`-uXdevW` both still
    run their script; keweichen, 2026-09-17). Returns ("terminal", 0) if
    Python exits before any script runs (-h/-V/--help/... anywhere in a
    cluster); ("script", 0) for -c/-m; ("value", extra) for -W/-X or
    --check-hash-based-pycs, extra=1 when the value is a separate NEXT
    token, else 0; ("sep", 0) for `--`; ("skip", 0) for a recognized
    valueless flag; or (None, 0) when the token is unknown/unrecognized --
    fail closed rather than guess past what the real grammar doesn't cover."""
    if tok == "--":
        return ("sep", 0)
    if not tok.startswith("-") or tok == "-":
        return (None, 0)
    if tok.startswith("--"):
        if tok in _TERMINAL_LONG:
            return ("terminal", 0)
        if tok in _VALUE_LONG:
            return ("value", 1)
        return (None, 0)  # unrecognized long option -- real Python errors too
    for pos, ch in enumerate(tok[1:], start=1):
        if ch in _TERMINAL_CHARS:
            return ("terminal", 0)
        if ch in _SCRIPT_CHARS:
            return ("script", 0)
        if ch in _VALUE_CHARS:
            return ("value", 0 if pos + 1 < len(tok) else 1)
        if ch not in _VALUELESS_CHARS:
            return (None, 0)  # unrecognized char in the cluster -- fail closed
    return ("skip", 0)


def _segment_invokes(seg: str, name: str) -> bool:
    toks = _command_tokens(seg)
    if toks and toks[0] in ("bash", "sh", "source", ".") and len(toks) > 1:
        toks = toks[1:]
    return bool(toks) and toks[0].split("/")[-1] == name


def _segment_python_arg(seg: str):
    toks = _command_tokens(seg)
    if not toks or toks[0] not in ("python3", "python"):
        return None
    rest = toks[1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":
        tok = rest[i]
        kind, extra = _option_kind(tok)
        if kind is None or kind in ("terminal", "script"):
            return None  # no script operand exists in this shape
        # An out-of-domain value for this long option errors too -- no script runs.
        if tok in _VALUE_LONG and (i + 1 >= len(rest) or rest[i + 1] not in _HASH_PYCS_VALUES):
            return None
        i += 1 + extra
        if kind == "sep":
            break  # `--` ends option scanning; the next token is positional
    if i < len(rest) and rest[i].endswith(".py"):
        return rest[i]
    return None


def invokes(line: str, name: str) -> bool:
    """True when `name` runs in COMMAND position on this line.

    Position, not presence: `echo "x.sh"` has the name as an ARGUMENT, an
    assignment `T=x.sh` runs nothing, and `not-x.sh` merely ends with it.
    One physical line: a program spanning lines needs program_invokes()."""
    return any(_segment_invokes(seg, name) for seg in _segments(_strip_comment(line)))


def program_invokes(text: str, name: str) -> bool:
    """invokes() over a WHOLE program, so AND-OR state survives line breaks.

    `false &&` at the end of one line guards the command on the next; scanning
    the lines one at a time credited that command (measured: both real
    consumers did, and Bash returned 0 with the planted test never run)."""
    return any(_segment_invokes(seg, name) for seg in _segments(active_text(text)))


def python_args(line: str) -> list[str]:
    """The SCRIPT `.py` argument to python3/python in COMMAND position.

    `-c`/`-m` invocations have no script file to credit — everything after
    them is inline code or a module name, and a `.py`-looking token that
    follows is the script's OWN argv, never something python loads (measured
    false positive: `python3 -c 'pass' packages/x/test_argv.py` used to name
    test_argv.py as invoked). Only the first non-flag token counts as the
    script, so a later `.py`-looking argument is never credited either.
    `echo python3 x.py` names x.py as an ARGUMENT to echo, not a caller —
    same position discipline as `invokes()`, extracting instead of testing.
    One physical line: a program spanning lines needs program_python_args()."""
    out = []
    for seg in _segments(_strip_comment(line)):
        a = _segment_python_arg(seg)
        if a:
            out.append(a)
    return out


def program_python_args(text: str) -> list[str]:
    """python_args() over a WHOLE program; see program_invokes()."""
    out = []
    for seg in _segments(active_text(text)):
        a = _segment_python_arg(seg)
        if a:
            out.append(a)
    return out


def _if_head(seg: str) -> str:
    """'false'/'true' for a literal-constant `if`, else 'other' (unknown to us)."""
    toks = seg.split()
    if len(toks) >= 2 and toks[1] in ("false", "true"):
        return toks[1]
    return "other"


def _filter_dead_branches(segments: list[str]) -> list[str]:
    """Drop segments inside an `if false`/`if true` branch that provably never runs.

    Only a literal constant condition is decidable without a real shell, so
    `elif`/any other `if <cond>` leaves both its branches in — credited, not
    proven reachable, but never wrongly dropped either. `if`/`then`/`else`/
    `elif`/`fi` are markers, but Bash allows a real command glued onto the
    SAME segment (`if true; then python3 x.py; fi`) -- peel the keyword and
    keep analyzing the remainder under the branch's own drop state, rather
    than discarding the whole segment (measured false negative: qingyun-wu +
    keweichen, 2026-09-17, both `then` and `else` glued forms). A glued
    remainder can itself open a NESTED `if` on the same split ('then if
    false; ...'); ALWAYS re-dispatch it, live or dead, so a nested `if`'s
    own `fi` pops ITS frame and not the outer one -- skipping the dispatch
    while dead left a later sibling command read as reachable again
    (measured: keweichen, 2026-09-17, both the never-pushed-frame and the
    reachable-sibling-after-a-dead-nested-if shapes)."""
    out, stack, pending = [], [], list(segments)
    while pending:
        seg = pending.pop(0)
        toks = seg.split(maxsplit=1)
        head = toks[0] if toks else ""
        rest = toks[1] if len(toks) > 1 else ""
        if head == "if":
            parent_drop = stack[-1]["drop"] if stack else False
            kind = _if_head(seg)
            stack.append({"kind": kind, "drop": parent_drop or kind == "false"})
            continue
        if head == "elif" and stack:
            stack[-1] = {"kind": "other", "drop": stack[-2]["drop"] if len(stack) > 1 else False}
            continue
        if head == "then" and stack:
            if rest:
                pending.insert(0, rest)
            continue
        if head == "else" and stack:
            frame, parent_drop = stack[-1], (stack[-2]["drop"] if len(stack) > 1 else False)
            frame["drop"] = True if frame["kind"] == "true" else parent_drop
            if rest:
                pending.insert(0, rest)
            continue
        if head == "fi" and stack:
            stack.pop()
            continue
        if not (stack and stack[-1]["drop"]):
            out.append(seg)
    return out


def _segments(line: str):
    return _filter_dead_branches(_raw_segments(line))


def _sets_pipefail(text: str) -> "bool | None":
    """True/False when `text` is a `set` invocation toggling pipefail (any
    combined short-opt cluster ending in an `o` that takes `pipefail`, e.g.
    `-o`/`-eo`/`-euo`; `+o` disables), else None -- not pipefail-relevant."""
    toks = text.split()
    if not toks or toks[0] != "set":
        return None
    rest = " ".join(toks[1:])
    if re.search(r"(?:^|\s)\+[A-Za-z]*o\s+pipefail(?:\s|$)", rest):
        return False
    if re.search(r"(?:^|\s)-[A-Za-z]*o\s+pipefail(?:\s|$)", rest):
        return True
    return None


def _raw_segments(line: str):
    """Split into AND-OR lists on UNCONDITIONAL separators, keeping each
    list's FIRST command and, when decidable, the ones after it too.

    `cmd1 && cmd2` may skip cmd2 depending on cmd1's exit status, so cmd2 is
    credited only when that status is KNOWN without a real shell -- cmd1 is
    the literal bare word `true` (for `&&`) or `false` (for `||`); anything
    else stays undecidable and cmd2 is dropped (measured false positive:
    `false && python3 packages/x/test_dead.py || true` used to name
    test_dead.py as invoked, though Bash never runs it -- and it must stay
    that way here, since `false` is not `true`). This chains: once a segment
    is undecidable, every later one in the same list is too, until a hard
    reset (measured false negative: `true && python3 x.py` never credited
    x.py at all -- qingyun-wu + keweichen, the one gap named and deferred
    through every earlier round of this file). `;` and a lone `&`
    (background) don't gate on exit status, so each command they separate
    is its own independently-scanned, freshly-reachable list.

    A lone `|` (pipe) is NOT one of those hard resets: `cmd1 | cmd2` is one
    syntactic unit, so a `&&`/`||` gating cmd1 gates the whole pipe, not just
    its first stage (measured false positive, keweichen round 11: `false &&
    printf x | python3 dead.py` used to credit dead.py, because the pipe was
    treated as freshly-reachable the moment the guard's own segment ended).
    Each stage still runs once the pipe is entered, and the pipe's own
    resulting status feeds the NEXT `&&`/`||`: without `pipefail` that's the
    LAST stage's status; with it active (a reached `set -o/-eo/-euo
    pipefail`, sticky for the rest of the program), any known-failing stage
    wins over a later success (measured child regression, same round: `set
    -o pipefail; false | true && python3 dead.py` started crediting dead.py,
    though Bash's pipefail-adjusted exit is `false`'s and dead.py never
    runs). A pipe the guard skipped entirely contributes nothing of its own;
    the gate's already-known status passes through unchanged.

    Inside single quotes nothing is special, backslash included: `'a\\'`
    is a 2-char literal, not an escaped, still-open quote.

    Multi-line input is one program: an unquoted newline ends an open command
    the way `;` does, but a line that ended right after `&&`/`||` leaves the
    next line's command under that guard, and backslash-newline joins."""
    # Named above: &&/|| gate on pending_op/chain_status; the open pipe on
    # last_runs/pipe_group/pipe_entered; pipefail_on is the sticky `set` toggle.
    out, cur, quote, i = [], [], None, 0
    pending_op = None
    chain_status = None
    last_runs = True
    pipe_group = []
    pipe_entered = True
    pipefail_on = False

    def transition(sep):
        nonlocal cur, chain_status, pending_op, last_runs
        nonlocal pipe_group, pipe_entered, pipefail_on
        s = "".join(cur)
        cur = []
        text = s.strip()
        if text:
            if pending_op is None:
                runs = True
            elif pending_op == "|":
                runs = last_runs
            else:
                runs = chain_status == ("true" if pending_op == "&&" else "false")
            if runs:
                out.append(s)
                pf = _sets_pipefail(text)
                if pf is not None:
                    pipefail_on = pf
            last_runs = runs
            shape = text if (runs and text in ("true", "false")) else "unknown"
            if pending_op == "|":
                pipe_group.append(shape)
            else:
                pipe_group = [shape]
                pipe_entered = runs
        if sep == "|":
            pending_op = "|"
            return
        if pipe_entered and pipe_group:
            if pipefail_on and "false" in pipe_group:
                chain_status = "false"
            elif pipefail_on and all(x == "true" for x in pipe_group):
                chain_status = "true"
            elif not pipefail_on:
                chain_status = pipe_group[-1]
            else:
                chain_status = "unknown"
        # else: the pipe never ran, or there was none -- chain_status (the
        # gate's own already-known status) passes through unchanged.
        pipe_group = []
        if sep in ("&&", "||"):
            pending_op = sep
        else:                       # ";", "&", or end-of-input: a fresh list starts
            chain_status = None
            last_runs = True
            pipe_entered = True
            pending_op = None

    while i < len(line):
        ch = line[i]
        nxt = line[i + 1] if i + 1 < len(line) else ""
        if ch == "\\" and quote != "'" and nxt == "\n":
            i += 2; continue  # line continuation: one logical line
        if ch == "\\" and quote != "'" and i + 1 < len(line):
            cur.append(ch); cur.append(line[i + 1]); i += 2; continue
        if ch == "\n" and not quote:
            if "".join(cur).strip():
                transition(";")
            i += 1; continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1; continue
        if ch in "'\"":
            quote = ch; cur.append(ch); i += 1; continue
        if ch in "&|" and nxt == ch:
            transition(ch + ch); i += 2; continue
        if ch in ";|&":
            transition(ch); i += 1; continue
        cur.append(ch); i += 1
    transition(None)
    return [s for s in out if s.strip()]
