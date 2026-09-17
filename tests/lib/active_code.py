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


def _option_kind(tok: str):
    """Classify a short-option cluster by its FIRST value-taking letter,
    scanned in order -- ownership, not a fixed position, decides attached
    vs. separate. Returns ("script", 0) for -c/-m (consumes the rest of the
    command line, per Python CLI rules); ("value", extra) for -W/-X, extra=0
    when a value is attached right after that letter in the SAME token
    (`-uWX` -- W owns "X"), extra=1 when nothing follows (the value is the
    NEXT token, bare or clustered: `-W`, `-uW`); or (None, 0) otherwise
    (measured: `-uWX`/`-uXdevW` both still run their script, but a
    last-char-only test misread each as taking a separate value it doesn't
    own -- keweichen, 2026-09-17)."""
    if not tok.startswith("-") or tok.startswith("--") or tok == "-":
        return (None, 0)
    for pos, ch in enumerate(tok[1:], start=1):
        if ch in "cm":
            return ("script", 0)
        if ch in "WX":
            return ("value", 0 if pos + 1 < len(tok) else 1)
        if not ch.isalpha():
            break
    return (None, 0)


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
    while i < len(rest) and rest[i].startswith("-") and rest[i] not in ("-", "--"):
        kind, extra = _option_kind(rest[i])
        if kind == "script":
            return None  # no script operand exists in this shape
        i += 1 + extra
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


def _raw_segments(line: str):
    """Split into AND-OR lists on UNCONDITIONAL separators, keeping only each
    list's FIRST command — the only one Bash is guaranteed to reach.

    `cmd1 && cmd2` may skip cmd2 depending on cmd1's exit status, so cmd2 is
    never credited as invoked (measured false positive: `false && python3
    packages/x/test_dead.py || true` used to name test_dead.py as invoked,
    though Bash never runs it). `;`, a lone `&` (background), and a lone `|`
    (pipe) don't gate on exit status, so each command they separate is its
    own independently-scanned list — and a `;` after a `&&`/`||` chain ends
    the conditional run, so what follows it is unconditional again.

    Inside single quotes nothing is special, backslash included: `'a\\'`
    is a 2-char literal, not an escaped, still-open quote.

    Multi-line input is one program: an unquoted newline ends an open command
    the way `;` does, but a line that ended right after `&&`/`||` leaves the
    next line's command under that guard, and backslash-newline joins."""
    out, cur, quote, i, conditional = [], [], None, 0, False

    def flush():
        nonlocal cur
        s = "".join(cur)
        if s.strip() and not conditional:
            out.append(s)
        cur = []

    while i < len(line):
        ch = line[i]
        nxt = line[i + 1] if i + 1 < len(line) else ""
        if ch == "\\" and quote != "'" and nxt == "\n":
            i += 2; continue  # line continuation: one logical line
        if ch == "\\" and quote != "'" and i + 1 < len(line):
            cur.append(ch); cur.append(line[i + 1]); i += 2; continue
        if ch == "\n" and not quote:
            if "".join(cur).strip():
                flush(); conditional = False
            i += 1; continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1; continue
        if ch in "'\"":
            quote = ch; cur.append(ch); i += 1; continue
        if ch in "&|" and nxt == ch:
            flush(); conditional = True; i += 2; continue
        if ch in ";|&":
            flush(); conditional = False; i += 1; continue
        cur.append(ch); i += 1
    flush()
    return [s for s in out if s.strip()]
