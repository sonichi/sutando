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
    """`seg`'s tokens with a leading env/VAR=/`!` prefix peeled off.

    `FOO=1 cmd` runs `cmd`; `BAD-NAME=1 cmd` is not a valid assignment (a
    hyphen can't start a shell identifier), so bash tries to RUN it and
    fails — peeling it here would wrongly credit `cmd` as invoked. `!`
    negates the reported STATUS only; the command after it still runs
    (keweichen round 13, confirmed by direct execution: `! python3 x.py`
    really executes python3 on both Bash 3.2 and 5.2) -- but ONLY the raw,
    unquoted, unescaped reserved word in the pipeline's own leading position:
    `'!' cmd`, `\\! cmd`, `env ! cmd` and `X=1 ! cmd` all try to RUN a
    program literally named `!` and fail with 127 on both Bash 3.2 and 5.2
    (keweichen round 15) -- shlex already erased the quoting/escaping by the
    time tokens exist, so this checks the untokenized text first."""
    stripped = seg.lstrip()
    leading_bang = stripped == "!" or stripped[:2] in ("! ", "!\t")
    import shlex
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    if leading_bang and toks and toks[0] == "!" and len(toks) > 1:
        toks = toks[1:]
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


def _heredoc_delimiters(line: str):
    """Each valid `<<`/`<<-` heredoc's (delimiter_text, strip_tabs) on this
    line, left to right, quote-aware. A delimiter-less `<<` (no word at
    all) contributes nothing -- there is no body to consume, and its own
    line already carries the fatal verdict via `_shell_words`. `<<<`
    (here-string) is a different operator with no body and is skipped.
    `<<` inside `((...))`/`$((...))` arithmetic is a left-shift, not a
    redirect (round 24: `x=$((1 << 2))` was misread as one) -- but a
    NESTED `$(...)` command substitution re-enters a real command
    context even while still lexically inside the arithmetic's parens
    (round 25, keweichen: a heredoc inside `$(( $(cmd <<EOF ...) ))` was
    wrongly suppressed too), so a stack tracks each open group's KIND
    (`arith`/`cmdsub`/plain grouping `(`) and only the nearest kind that
    isn't a plain grouping paren decides whether `<<` is a shift."""
    out, i, n, quote = [], 0, len(line), None
    stack = []

    def in_arith():
        for kind, _ in reversed(stack):
            if kind != "paren":
                return kind == "arith"
        return False

    while i < n:
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
            i += 1
            continue
        if ch in "'\"":
            quote = ch; i += 1; continue
        if ch == "\\" and i + 1 < n:
            i += 2; continue
        if ch == "$" and i + 1 < n and line[i + 1] == "(":
            if i + 2 < n and line[i + 2] == "(":
                stack.append(["arith", 2]); i += 3
            else:
                stack.append(["cmdsub", 1]); i += 2
            continue
        if ch == "(":
            if i + 1 < n and line[i + 1] == "(":
                stack.append(["arith", 2]); i += 2
            else:
                stack.append(["paren", 1]); i += 1
            continue
        if ch == ")":
            if stack:
                stack[-1][1] -= 1
                if stack[-1][1] <= 0:
                    stack.pop()
            i += 1; continue
        if in_arith() and ch == "<" and i + 1 < n and line[i + 1] == "<":
            i += 2; continue
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            if i + 2 < n and line[i + 2] == "<":
                i += 3; continue  # here-string, not a heredoc
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True; j += 1
            while j < n and line[j] in " \t":
                j += 1
            delim, word_started = [], False
            while j < n and line[j] not in " \t\n":
                word_started = True
                if line[j] == "'":
                    j += 1
                    while j < n and line[j] != "'":
                        delim.append(line[j]); j += 1
                    j += 1
                elif line[j] == '"':
                    j += 1
                    while j < n and line[j] != '"':
                        if line[j] == "\\" and j + 1 < n:
                            delim.append(line[j + 1]); j += 2
                        else:
                            delim.append(line[j]); j += 1
                    j += 1
                elif line[j] == "\\" and j + 1 < n:
                    delim.append(line[j + 1]); j += 2
                else:
                    delim.append(line[j]); j += 1
            if word_started:
                out.append(("".join(delim), strip_tabs))
            i = j
            continue
        i += 1
    return out


def _strip_heredoc_bodies(text: str) -> str:
    """Heredoc BODY lines are literal data fed to the redirect, never
    command text -- drop them (and their terminator) before any
    command-position scan sees them (round 23: a body line that merely
    LOOKS like a command, e.g. `set -o pipefail <<`, was scanned as one,
    including into the whole-program fatal halt it happened to resemble).
    Comment-stripping and blank-line dropping happen HERE, on code lines
    only, rather than by a separate `active_text()` pre-pass -- doing
    that first destroys a blank or `#`-shaped heredoc TERMINATOR before
    this scan ever sees it (round 25, keweichen: heredoc recognition and
    comment/blank filtering need one lexical owner, not two in sequence)."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        code = _strip_comment(lines[i])
        if code.strip():
            out.append(code)
        for delim, strip_tabs in _heredoc_delimiters(code):
            i += 1
            while i < len(lines):
                probe = lines[i].lstrip("\t") if strip_tabs else lines[i]
                if probe == delim:
                    break
                i += 1
        i += 1
    return "\n".join(out)


_FUNC_START_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w.-]*)\s*\(\s*\)\s*\{\s*(.*)$")
# The `{` on its OWN line -- a valid spelling `_FUNC_START_RE` alone can't
# see, since it anchors the `{` to the same line as `name()`.
_FUNC_START_SPLIT_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w.-]*)\s*\(\s*\)\s*$")
# The `function` keyword with NO parens at all -- also valid Bash, and
# distinct from the two forms above (which both require `()`).
_FUNC_START_KEYWORD_RE = re.compile(r"^\s*function\s+([A-Za-z_][\w.-]*)\s*\{\s*(.*)$")
_FUNC_START_KEYWORD_SPLIT_RE = re.compile(r"^\s*function\s+([A-Za-z_][\w.-]*)\s*$")
_BRACE_ONLY_RE = re.compile(r"^\s*\{\s*$")
_CLOSE_ONLY_RE = re.compile(r"^\s*\}\s*$")
_CMD_SEP = frozenset(";&|")


def _brace_delta(line: str) -> int:
    """Net `{`/`}` depth change, quote- and `${...}`-aware — a parameter
    expansion's OWN braces (opener, any nested ones, and its closer) never
    open or close a function block, so the whole span is skipped as a unit,
    not just its opener (round 30b, keweichen: `echo ${x}` left the closer
    uncounted, so its `}` alone closed an unrelated function block early).
    A backslash-escaped brace (`\\{`/`\\}`, e.g. `${x:-\\}}`'s literal
    default) is inert everywhere -- round 33, keweichen: unescaped, that
    `\\}` read as the expansion's OWN closer, so the REAL closer fell
    through to the outer counter and closed an unrelated function early.
    A bare `{`/`}` is a reserved word ONLY in COMMAND-START position, as
    its own whitespace-delimited token -- round 33, keweichen:
    `echo hi } more` is `}` as a plain ARGUMENT to echo (never reached as
    a command), but counting every brace character regardless of position
    closed the enclosing function one line early."""
    masked = unquoted(line)
    delta, i, n = 0, 0, len(masked)
    at_cmd_start = True  # the start of a physical line is itself command-start
    while i < n:
        ch = masked[i]
        if ch == "\\" and i + 1 < n and masked[i + 1] in "{}":
            i += 2
            at_cmd_start = False
            continue
        if ch == "$" and i + 1 < n and masked[i + 1] == "{":
            depth, i = 1, i + 2
            while i < n and depth > 0:
                if masked[i] == "\\" and i + 1 < n and masked[i + 1] in "{}":
                    i += 2
                    continue
                if masked[i] == "{":
                    depth += 1
                elif masked[i] == "}":
                    depth -= 1
                i += 1
            at_cmd_start = False
            continue
        if ch in "{}":
            is_token = (i == 0 or masked[i - 1].isspace()) and (i + 1 == n or masked[i + 1].isspace())
            if at_cmd_start and is_token:
                delta += 1 if ch == "{" else -1
            at_cmd_start = False
            i += 1
            continue
        if ch in _CMD_SEP:
            at_cmd_start = True
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        at_cmd_start = False
        i += 1
    return delta


def _function_bodies(text: str) -> list[tuple[str, int, tuple[int, ...], int, int]]:
    """(name, anchor, sig_lines, first_body_line, last_body_line) for every
    `name() { ... }` or `function name { ... }` definition (either
    spelling, `{` on the same line or its own next line) — 0-based
    indices into `text.split("\\n")`. `anchor` is the definition's own
    first line, for ordering redefinitions against call sites. `sig_lines`
    are the pure declaration line(s) (`name() {` or `name()` + `{`) that
    must never be scanned as call text -- round 33, keweichen: `discover ()
    {` (a space before the parens) tokenizes its OWN line as a bare call to
    "discover", which the no-space spelling only avoided by tokenization
    luck (`discover()` glues into one token that can't equal the name).

    Multi-line spans exclude a clean closing-brace-only line, but INCLUDE
    it when real content shares that line (round 33, keweichen: `bash
    helper.sh; }` put the last command on the same line as the closer,
    outside every recorded span, so an uncalled function's last command
    read as top-level). A one-liner (`name() { cmd; }`, closed on its own
    opening line) has no separate line to exclude, so its span is that
    single line and `sig_lines` is empty (round 33, keweichen: treating it
    as having "no line to hide" left its own line out of every function's
    tracked span entirely, crediting an uncalled one-liner as top-level)."""
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        m = _FUNC_START_RE.match(lines[i]) or _FUNC_START_KEYWORD_RE.match(lines[i])
        if m:
            rest, brace_line, anchor, sig_lines = m.group(2), i, i, (i,)
        else:
            m = _FUNC_START_SPLIT_RE.match(lines[i]) or _FUNC_START_KEYWORD_SPLIT_RE.match(lines[i])
            if m and i + 1 < n and _BRACE_ONLY_RE.match(lines[i + 1]):
                rest, brace_line, anchor, sig_lines = "", i + 1, i, (i, i + 1)
            else:
                i += 1
                continue
        depth = 1 + _brace_delta(rest)
        if depth <= 0:
            out.append((m.group(1), anchor, (), brace_line, brace_line))
            i = brace_line + 1
            continue
        has_rest = bool(rest.strip())
        j = brace_line + 1
        while j < n and depth > 0:
            depth += _brace_delta(lines[j])
            j += 1
        close_line = j - 1
        end = close_line - 1 if _CLOSE_ONLY_RE.match(lines[close_line]) else close_line
        # `rest` on the opener is body content too -- a function with no
        # line past it before a bare `}` needs the opener IN its own span.
        start = brace_line if has_rest else brace_line + 1
        if end >= start:
            out.append((m.group(1), anchor, () if has_rest else sig_lines, start, end))
        i = j
    return out


def _segment_calls(seg: str, name: str) -> bool:
    toks = _command_tokens(seg)
    return bool(toks) and toks[0] == name


def _strip_unreachable_function_bodies(text: str) -> str:
    """A defined-but-never-CALLED function's body must not credit an
    invocation: `foo() { bash discover.sh; }` with no call to `foo`
    anywhere never runs discover.sh (round 30, keweichen — the delegation
    guard credited exactly this shape). Reachability is transitive: a body
    called from an already-reachable function counts too, not just the
    top-level flow outside every function.

    A call resolves to whichever definition of that NAME was already
    ANCHORED (its own first line) before the CALLING CONTEXT's own point of
    execution -- Bash redefinition is last-wins only AT THAT POINT, never
    globally (round 33, keweichen: a decoy defined AFTER a real call still
    "won" under a single global last-definition rule, and the inverse --
    a helper defined and called BEFORE a later decoy -- wrongly lost credit
    for a call that had already run). A call before every definition of
    that name resolves to nothing, matching a real `command not found`.

    A call INSIDE a function's own body resolves against whichever line
    reached that function's OWN invocation, not the nested call's fixed
    textual position in the caller's body -- running a body never advances
    the script's sequential position, so a name used inside `outer` binds
    exactly as if written at `outer`'s own call site (round 33 follow-up,
    kewei-red-ag2space: resolving against the nested call's own line instead
    credited whichever definition preceded it in SOURCE order, which is
    always the STALEST one, regardless of what a later redefinition --
    reached before `outer` was ever invoked -- would really run).

    A same-line function-open ("name() { CMD" or "name() { CMD; }") glues
    its body to the declaration syntax on one physical line; that line is
    unmasked to just CMD before any call-scan sees it (round 33 follow-up:
    `discover() { helper; }` then `discover` still read as calling nothing,
    since "discover() { helper" tokenizes its own first word as
    "discover()", never "helper"). A candidate line's own call is credited
    only when it also survives the SAME multi-line AND-OR/if dead-branch
    state the whole candidate region would see, not just its own isolated
    text -- a per-line check alone can't tell `false &&\n  discover` or
    `if false; then\n  discover\nfi` from an unconditional call."""
    lines = text.split("\n")
    funcs = _function_bodies(text)
    if not funcs:
        return text

    # A same-line opener's body is glued to its declaration syntax; unmask
    # it to just the body text before any call-scan sees the line.
    for _, anchor, _, start, _ in funcs:
        if start == anchor:
            m = _FUNC_START_RE.match(lines[start]) or _FUNC_START_KEYWORD_RE.match(lines[start])
            if m:
                lines[start] = m.group(2)

    # Every signature/body line is off-limits for call-scanning regardless
    # of reachability -- a declaration is never a call (see `discover ()`).
    excluded = [False] * len(lines)
    for _, _, sig_lines, start, end in funcs:
        for k in sig_lines:
            excluded[k] = True
        for k in range(start, end + 1):
            excluded[k] = True
    top_level_idx = [idx for idx in range(len(lines)) if not excluded[idx]]

    by_name: dict[str, list[tuple[int, int, int]]] = {}
    for name, anchor, _, start, end in funcs:
        by_name.setdefault(name, []).append((anchor, start, end))
    for spans in by_name.values():
        spans.sort()

    def called_on(name: str, idxs: list[int]) -> list[int]:
        """Line indices among `idxs` whose own line calls `name`, subject
        to the SAME multi-line AND-OR/if dead-branch state the whole
        candidate region would see -- a per-line check alone can't tell
        `false &&\\n  discover` or `if false; then\\n  discover\\nfi` from
        an unconditional call (kewei-red-ag2space round 33 follow-up).
        Lines outside `idxs` are blanked, not omitted, so a gap (an
        excluded function body sitting between two candidates) can't
        shift adjacency; a blank line is a no-op to the AND-OR/if scanner,
        exactly like a line that was never there."""
        if not idxs:
            return []
        idx_set = set(idxs)
        region = [lines[k] if k in idx_set else "" for k in range(len(lines))]
        live = _segments("\n".join(region))
        live_pos, out = 0, []
        for idx in idxs:
            own = _segments(lines[idx])
            matched = []
            for seg in own:
                if live_pos < len(live) and live[live_pos] == seg:
                    matched.append(seg)
                    live_pos += 1
            if any(_segment_calls(seg, name) for seg in matched):
                out.append(idx)
        return out

    def resolve(name: str, at_line: int) -> "tuple[int, int] | None":
        """The (start, end) of `name`'s definition active at `at_line` --
        the latest anchor strictly before it -- or None if none precedes."""
        best = None
        for anchor, start, end in by_name.get(name, ()):
            if anchor < at_line:
                best = (start, end)
        return best

    # `at_line` is the call site that reached each queued function, not
    # the nested call's own body-line position (see the docstring above).
    reachable: set[tuple[int, int]] = set()
    queue: list[tuple[int, int, int]] = []
    for name in by_name:
        for idx in called_on(name, top_level_idx):
            span = resolve(name, idx)
            if span and span not in reachable:
                reachable.add(span)
                queue.append((span[0], span[1], idx))
    while queue:
        cstart, cend, at_line = queue.pop()
        body_idx = list(range(cstart, cend + 1))
        for name in by_name:
            for idx in called_on(name, body_idx):
                span = resolve(name, at_line)
                if span and span not in reachable:
                    reachable.add(span)
                    queue.append((span[0], span[1], at_line))

    for _, _, _, start, end in funcs:
        if (start, end) in reachable:
            continue
        for k in range(start, end + 1):
            lines[k] = ""
    return "\n".join(lines)


def program_invokes(text: str, name: str) -> bool:
    """invokes() over a WHOLE program, so AND-OR state survives line breaks.

    `false &&` at the end of one line guards the command on the next; scanning
    the lines one at a time credited that command (measured: both real
    consumers did, and Bash returned 0 with the planted test never run)."""
    return any(_segment_invokes(seg, name)
               for seg in _segments(_strip_unreachable_function_bodies(_strip_heredoc_bodies(text))))


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
    """python_args() over a WHOLE program; see program_invokes() — same
    unreachable-function-body exclusion, so an uncalled helper's `python3 x.py` isn't credited either."""
    out = []
    for seg in _segments(_strip_unreachable_function_bodies(_strip_heredoc_bodies(text))):
        a = _segment_python_arg(seg)
        if a:
            out.append(a)
    return out


_MASK_AND = "\x01\x02"
_MASK_OR = "\x03\x04"


def _mask_if_conditions(text: str) -> str:
    """Blind `_raw_segments()` to a `&&`/`||` inside an `if`/`elif`
    CONDITION -- those belong to the condition as ONE compound-list unit,
    not to top-level AND-OR chaining between separate commands (round 24:
    `if true && false; then ...` was split at the `&&`, corrupting the
    single word `_eval_condition()` needs to see). Restored there, not at
    the character-scan level, so `_raw_segments` never sees a real one.
    `if`/`elif`/`then` only count in COMMAND position -- right after a
    separator (`;`/`&`/`|`/`&&`/`||`/newline/start) or another keyword
    that itself opens one (`then`/`else`/`do`) -- never as an ordinary
    ARGUMENT (round 25, keweichen: `printf if` isn't an `if` statement,
    but masked the `||` after it anyway). Single-span, non-nested: a
    condition containing its own nested `if` is not handled (out of scope)."""
    out, i, n, quote, masking = [], 0, len(text), None, False
    at_cmd_start = True
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1; continue
        if ch in "'\"":
            quote = ch; out.append(ch); i += 1; at_cmd_start = False; continue
        if ch == "\\" and i + 1 < n:
            out.append(ch); out.append(text[i + 1]); i += 2; at_cmd_start = False; continue
        if masking and ch in "&|" and i + 1 < n and text[i + 1] == ch:
            out.append(_MASK_AND if ch == "&" else _MASK_OR); i += 2; at_cmd_start = True; continue
        if ch in ";\n" or (ch in "&|" and (i + 1 >= n or text[i + 1] != ch)):
            out.append(ch); i += 1; at_cmd_start = True; continue
        if ch in " \t":
            out.append(ch); i += 1; continue
        if ch.isalpha() and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            at_boundary = j >= n or not (text[j].isalnum() or text[j] == "_")
            was_cmd_start = at_cmd_start
            if at_boundary and was_cmd_start and word in ("if", "elif"):
                masking = True; at_cmd_start = True
            elif at_boundary and was_cmd_start and word == "then":
                masking = False; at_cmd_start = True
            elif at_boundary and was_cmd_start and word in ("else", "do"):
                at_cmd_start = True
            else:
                at_cmd_start = False
            out.append(word); i = j; continue
        out.append(ch); i += 1; at_cmd_start = False
    return "".join(out)


def _eval_condition(seg: str) -> str:
    """'false'/'true' for a literal-constant `if`/`elif` CONDITION, else
    'other' -- a `&&`/`||` chain of bare `true`/`false` is evaluated
    left to right the way a real Bash AND-OR list decides its exit status
    (round 24: `true && false; then` used to see only `true`, since
    `_raw_segments` split at the `&&` before this ever saw the `false`).
    A literal operand may carry leading `!` (round 28, qingyun-wu; round 29,
    keweichen): Bash's unary `!` negates a pipeline's own exit status --
    `if ! true` used to be unrecognized ('other'), crediting both arms of a
    branch Bash only ever takes one side of. A round-28 fix consumed only
    ONE `!` per operand on the claim that a second is a syntax error --
    that held on this host's Bash 3.2.57 but NOT on Bash 5.3.20 (confirmed
    live on both): `! ! true` there is valid and toggles twice, so the
    scanner must model the general case (each `!` toggles) rather than a
    single host's shell version."""
    toks = seg.replace(_MASK_AND, " && ").replace(_MASK_OR, " || ").split()
    if len(toks) < 2:
        return "other"
    rest = toks[1:]
    status, pending_op, i, n = None, None, 0, len(rest)
    while i < n:
        t = rest[i]
        if t in ("&&", "||"):
            if pending_op is not None:
                return "other"
            pending_op = t; i += 1; continue
        negate = False
        while i < n and rest[i] == "!":
            negate = not negate
            i += 1
        if i >= n:
            return "other"
        value = rest[i]; i += 1
        if status is None:
            runs = True
        elif pending_op == "&&":
            runs = status is True
        elif pending_op == "||":
            runs = status is False
        else:
            return "other"
        if runs:
            if value not in ("true", "false"):
                return "other"
            lit = value == "true"
            status = (not lit) if negate else lit
        pending_op = None
    if pending_op is not None or status is None:
        return "other"
    return "true" if status else "false"


def _filter_dead_branches(segments: list[str]) -> list[str]:
    """Drop segments inside an `if false`/`if true` branch that provably never runs.

    Only a literal constant condition is decidable without a real shell, so
    a non-literal `if`/`elif` leaves both its branches in — credited, not
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
    reachable-sibling-after-a-dead-nested-if shapes). `taken` tracks whether
    an EARLIER arm in the current if/elif/else chain already consumed the
    branch (True), definitely hasn't yet (False), or is undecidable
    ("unknown") -- once True, every later elif/else is dead regardless of
    ITS OWN condition. A non-literal arm poisons `taken` to "unknown", but
    an "unknown" arm followed by a GUARANTEED-true one still forces `taken`
    to True from there on: either the earlier arm consumed the branch, or
    it didn't and this one -- proven to run whenever reached -- surely did
    (round 25, keweichen: `elif true` after an undecidable `if` left a
    provably-dead trailing `else` still credited, since "unknown" used to
    propagate forever instead of resolving once a later arm is certain)."""
    out, stack, pending = [], [], list(segments)

    def _chain_transition(parent_drop, taken_in, kind):
        drop = parent_drop or taken_in is True or kind == "false"
        if taken_in is True or kind == "true":
            taken_out = True
        elif taken_in == "unknown" or kind == "other":
            taken_out = "unknown"
        else:
            taken_out = False
        return drop, taken_out

    while pending:
        seg = pending.pop(0)
        toks = seg.split(maxsplit=1)
        head = toks[0] if toks else ""
        rest = toks[1] if len(toks) > 1 else ""
        if head == "if":
            parent_drop = stack[-1]["drop"] if stack else False
            drop, taken = _chain_transition(parent_drop, False, _eval_condition(seg))
            stack.append({"drop": drop, "taken": taken})
            continue
        if head == "elif" and stack:
            parent_drop = stack[-2]["drop"] if len(stack) > 1 else False
            drop, taken = _chain_transition(parent_drop, stack[-1]["taken"], _eval_condition(seg))
            stack[-1] = {"drop": drop, "taken": taken}
            continue
        if head == "then" and stack:
            if rest:
                pending.insert(0, rest)
            continue
        if head == "else" and stack:
            parent_drop = stack[-2]["drop"] if len(stack) > 1 else False
            stack[-1]["drop"] = parent_drop or stack[-1]["taken"] is True
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
    return _filter_dead_branches(_raw_segments(_mask_if_conditions(line)))


def _split_unquoted_braces(val: list, qmask: list) -> "list | None":
    """One-level Bash brace expansion of an accumulated word, honouring
    PER-CHARACTER quote state rather than a whole-word flag -- round 18:
    `"p"ipe{fail,foo}` splits its quoting across the word (only the `p` is
    quoted) and Bash still expands the wholly-unquoted `{fail,foo}` that
    follows, which a whole-word `any_quoted` gate wrongly suppressed.
    Returns None (stays one literal word) when there is no unquoted
    `{a,b,...}` group -- no braces, a quoted delimiter, or `{x}` with no
    unquoted comma is not brace syntax at all."""
    n = len(val)
    for i in range(n):
        if val[i] != "{" or qmask[i]:
            continue
        j = i + 1
        close = -1
        while j < n:
            if val[j] == "{" and not qmask[j]:
                break  # nested brace -- not handled, try the next '{'
            if val[j] == "}" and not qmask[j]:
                close = j
                break
            j += 1
        if close == -1:
            continue
        parts, start, found_comma = [], i + 1, False
        for k in range(i + 1, close):
            if val[k] == "," and not qmask[k]:
                parts.append(val[start:k])
                start = k + 1
                found_comma = True
        parts.append(val[start:close])
        if not found_comma:
            continue
        pre, post = val[:i], val[close + 1:]
        return ["".join(pre + part + post) for part in parts]
    return None


def _brace_has_split_comma(val, qmask, text, i, n):
    """Does the brace group `val[0]=='{'` opened before position `i` hold
    an unquoted comma anywhere before its matching unquoted `}` -- checked
    on both sides of `i` (already-accumulated `val`, then a forward scan
    of `text`), since `_split_unquoted_braces` needs one ANYWHERE in the
    group, not only before the current scan position (round 25)."""
    depth = 1
    for k in range(1, len(val)):
        if qmask[k]:
            continue
        if val[k] == "{":
            depth += 1
        elif val[k] == "}":
            depth -= 1
            if depth == 0:
                return False
        elif val[k] == "," and depth == 1:
            return True
    quote, j = None, i
    while j < n:
        ch = text[j]
        if quote:
            if ch == quote:
                quote = None
            j += 1; continue
        if ch in "'\"":
            quote = ch; j += 1; continue
        if ch == "\\" and j + 1 < n:
            j += 2; continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return False
        elif ch == "," and depth == 1:
            return True
        j += 1
    return False


def _shell_words(text: str):
    """`text` as (value, expandable, any_quoted) triples. `expandable` is
    True iff the word carries a `$`/backtick that occurs unquoted or inside
    double quotes without an escaping backslash. `any_quoted` is True iff
    ANY part of the word was inside quotes at all. Adjacent quoted/unquoted
    spans concatenate into ONE word (Bash's own split-quoting rule, e.g.
    `'$'OPT` is one word). Exists because shlex.split() erases exactly the
    quoting distinction -- `'$OPT'`, `\\$OPT`, `"$OPT"` and bare `$OPT` all
    become the identical resolved token, though only the last two are ever
    expanded by Bash (keweichen rounds 15-17, confirmed by direct
    execution).

    A redirection (`[fd]>target`, `[fd]<target`, and their `>>`/`<&`/`<&`
    variants) is consumed by the shell and never reaches argv at all
    (round 18: `set -o >/dev/null pipefail` -- Bash removes the redirect
    before invoking `set`, which then sees only `-o pipefail`); a BARE
    (unquoted, unescaped) digit run immediately before the operator is its
    fd prefix, also consumed, while any other preceding text is a real word
    and gets flushed first (round 19: an escaped/quoted digit is a real
    argv word instead, since it can never be part of a live fd number). A
    STATICALLY (source-text) empty target aborts the WHOLE command before
    it runs at all -- this function returns None, not a word list with a
    gap in it (round 19: `set +o >"" pipefail` never reaches `set`) -- but
    a target carrying `$`/backtick decides success at RUNTIME, not from
    its source text, so this returns the literal string `"unknown"`
    instead of a list in that case (round 20: `>"$EMPTY"` is exactly as
    fatal as `>""` once $EMPTY expands, and looks nothing like it
    statically) -- BUT only when the command turns out to be `set`, decided
    only once the WHOLE word list is built (round 22: a redirect can
    precede the command word, so `words` may still be empty at the moment
    the redirect itself is seen -- round 21's earlier check at that point
    read a leading redirect's uncertainty as irrelevant and discarded it).
    `<<`/`<<-` heredocs are exempt from the empty-target rule entirely --
    an empty DELIMITER WORD is ordinary, valid heredoc syntax, not a failed
    open (round 20: `<<""` is not `<""`) -- but NO delimiter word at all
    (`<<` at end of line) is a real Bash PARSE error: the sentinel string
    `"fatal"` signals that nothing in the rest of the program ever runs,
    not merely that this command didn't (round 22: round 21's `return
    None` modeled it as "no state change", indistinguishable from a
    genuinely inert line). `<(cmd)`/`>(cmd)`
    process substitution is not a redirect at all; its word is marked
    expandable rather than copied in literally when glued to a
    flag-shaped prefix (round 22: an ORDINARY prefix like the `x` in
    `x<(true)` already guarantees a positional word via the option-scan
    stop rule below and needs no expandable-marking at all), since the
    real `/dev/fd/N` text is unknowable and a literal copy can smuggle an
    option-shaped character (`o`) into a cluster scan that never should
    have seen it (round 20: `<(echo o)`'s inner `o` was mistaken for a
    real `+o`) -- flag-shapedness is checked past a leading unquoted `{`
    (round 23: `{+u<(echo o),pipefail}` expands to a genuinely flag-shaped
    first word, but the brace hasn't been split yet at this point in the
    scan, so `val[0]` alone is `{`, not `+`).

    Brace expansion runs per accumulated word at flush time, tracked via a
    per-character quoted mask (round 18, `_split_unquoted_braces`) -- a
    whole-word `any_quoted` flag cannot tell a quoted delimiter from a
    quoted character elsewhere in the same split-quoted word."""
    words, val, qmask, expandable, any_quoted = [], [], [], False, False
    quote, i, n = None, 0, len(text)
    redirect_uncertain = False

    def flush():
        nonlocal val, qmask, expandable, any_quoted
        if val or any_quoted:
            cands = _split_unquoted_braces(val, qmask)
            if cands is None:
                words.append(("".join(val), expandable, any_quoted))
            else:
                for c in cands:
                    words.append((c, expandable, any_quoted))
        val, qmask, expandable, any_quoted = [], [], False, False

    while i < n:
        ch = text[i]
        if quote == "'":
            any_quoted = True
            if ch == "'":
                quote = None
            else:
                val.append(ch); qmask.append(True)
            i += 1
        elif quote == '"':
            any_quoted = True
            if ch == '"':
                quote = None
                i += 1
            elif ch == "\\" and i + 1 < n and text[i + 1] in "\"\\$`":
                val.append(text[i + 1]); qmask.append(True); i += 2  # escaped -- literal, not expandable
            else:
                if ch in "$`":
                    expandable = True
                val.append(ch); qmask.append(True); i += 1
        elif ch in " \t\n":
            flush(); i += 1
        elif ch in "<>" and i + 1 < n and text[i + 1] == "(":
            # Process substitution (see docstring) -- peek past a leading
            # `{` ONLY when its group will actually split (round 25).
            _skip = 1 if (val and val[0] == "{" and not qmask[0]
                          and _brace_has_split_comma(val, qmask, text, i, n)) else 0
            if len(val) > _skip and val[_skip] in "-+":
                expandable = True
            start = i; i += 2; depth = 1; pq = None
            while i < n and depth > 0:
                c2 = text[i]
                if pq:
                    if c2 == pq:
                        pq = None
                    elif pq == '"' and c2 == "\\" and i + 1 < n:
                        i += 1
                elif c2 in "'\"":
                    pq = c2
                elif c2 == "(":
                    depth += 1
                elif c2 == ")":
                    depth -= 1
                i += 1
            val.extend(text[start:i]); qmask.extend([True] * (i - start))
        elif ch in "<>":
            # An fd prefix must be BARE -- an escaped/quoted digit is a real
            # argv word instead (see docstring).
            is_bare_fd_or_nothing = (not val) or (
                all(c.isdigit() for c in val) and not any(qmask))
            if not is_bare_fd_or_nothing:
                flush()  # real word before the operator -- its own argv word
            else:
                val, qmask = [], []  # bare fd-prefix digits (or nothing) -- part of the redirect
            i += 1
            is_heredoc = False
            if i < n and text[i] == ch:
                is_heredoc = ch == "<"; i += 1
            elif i < n and text[i] == "&":
                i += 1
            if is_heredoc and i < n and text[i] == "-":
                i += 1  # `<<-` strips leading tabs from the body; irrelevant to the delimiter itself
            while i < n and text[i] in " \t":
                i += 1
            target, target_expandable, target_start = [], False, i
            while i < n and text[i] not in " \t\n":  # consume the target, quote-aware
                if text[i] == "'":
                    i += 1
                    while i < n and text[i] != "'":
                        target.append(text[i]); i += 1
                    i += 1
                elif text[i] == '"':
                    i += 1
                    while i < n and text[i] != '"':
                        if text[i] == "\\" and i + 1 < n:
                            target.append(text[i + 1]); i += 2
                        else:
                            if text[i] in "$`":
                                target_expandable = True
                            target.append(text[i]); i += 1
                    i += 1
                elif text[i] == "\\" and i + 1 < n:
                    target.append(text[i + 1]); i += 2
                else:
                    if text[i] in "$`":
                        target_expandable = True
                    target.append(text[i]); i += 1
            if is_heredoc:
                if i == target_start:
                    # NO delimiter word at all is a real syntax error --
                    # nothing in the rest of the PROGRAM runs (see docstring).
                    return "fatal"
                # else: any delimiter WORD, including an empty one, is valid heredoc syntax (round 20)
            elif target_expandable:
                # Deferred until the full word list is known (see docstring).
                redirect_uncertain = True
            elif not target:
                # An empty redirect target aborts the WHOLE command before
                # it runs (see docstring) -- so does this function.
                return None
        elif ch == "'":
            quote = "'"; i += 1
        elif ch == '"':
            quote = '"'; i += 1
        elif ch == "\\" and i + 1 < n:
            val.append(text[i + 1]); qmask.append(True); i += 2  # unquoted escape -- literal
        else:
            if ch in "$`":
                expandable = True
            val.append(ch); qmask.append(False); i += 1
    flush()
    # Decided only now that the complete word list exists (round 22): a
    # leading redirect leaves `words` empty at the point it's seen.
    if redirect_uncertain and words and words[0][0] == "set":
        return "unknown"
    return words


# Bash's own `set -o`/`+o` names (3.2 ∪ 5.x); an unrecognized value ABORTS
# the whole `set` (round 17). A name missing from THIS list aborts too -- confirmed inverted for `interactive-comments` (round 18).
_SET_O_NAMES = frozenset((
    "allexport", "braceexpand", "emacs", "errexit", "errtrace", "functrace",
    "hashall", "histexpand", "history", "ignoreeof", "interactive-comments",
    "keyword", "monitor", "noclobber", "noexec", "noglob", "nolog", "notify",
    "nounset", "onecmd", "physical", "pipefail", "posix", "privileged",
    "verbose", "vi", "xtrace",
))


def _sets_pipefail(text: str) -> "bool | str | None":
    """The pipefail state a `set` invocation leaves ON, "unknown" when a
    toggle's value cannot be resolved statically, "fatal" when `text` is
    itself an unparseable syntax error (round 22), or None when `text` is
    not `set`, or is `set` with nothing pipefail-relevant at all.

    `o` anywhere in a `-`/`+` short-opt cluster ALWAYS consumes the next
    whole word as its value, regardless of the cluster's other letters --
    keweichen round 12, confirmed by direct execution: `set -oe pipefail`
    and `set -euo pipefail` both enable it exactly like `-o`/`-eo` do.
    Multiple `-o`/`+o pipefail` toggles apply in argv order -- Bash
    re-evaluates each left to right -- so the LAST one found wins, UNLESS
    an earlier one names an unrecognized option: that aborts the whole
    invocation before any later toggle is reached (round 17).

    Scanning stops at `--` OR a lone `-` (both are Bash's own end-of-options
    markers for `set`) OR at the first word that isn't `-`/`+`-shaped at
    all (`set -o pipefail positional +o pipefail` leaves it ON) -- and a
    value containing `$`/backtick may resolve to "pipefail" at runtime and
    we cannot know without a real shell -- "unknown" propagates that
    honestly, checked per-WORD via `_shell_words()` rather than by a
    whole-command substring search, which cannot tell two occurrences of
    the identical resolved text apart when only one of them is the value a
    `-o` actually consumed (round 17: `set -o "$OPT" '$OPT'` enables
    pipefail from the FIRST, expandable occurrence; a substring check for
    a literal copy of "$OPT" anywhere in the command found the SECOND one
    and wrongly called the whole thing literal). Brace expansion is
    resolved by `_shell_words()` itself, one argv word per candidate (round
    18: `set +o {errexit,pipefail}` only ever hands `+o` the FIRST expanded
    word -- `pipefail` next to it is a separate, unflagged word that ends
    `set`'s own option scanning like any bare positional, so pipefail
    stays ON here, not off). An empty word (`set "" +o pipefail`) is a
    real positional argument too, not the absence of one -- it ends
    scanning exactly like a non-empty one would (round 18). A redirect
    whose TARGET's runtime value (not its source text) decides success
    -- one carrying `$`/backtick -- makes the whole line's effect on
    pipefail unknowable too, and `_shell_words()` signals that by
    returning the literal string `"unknown"` rather than a word list
    (round 20: `>"$EMPTY"` looks non-empty lexically and is empty at
    runtime, same failure as a literal `>""`, just not visible here)."""
    words = _shell_words(text)
    if words == "unknown":
        return "unknown"
    if words == "fatal":
        return "fatal"
    if not words or words[0][0] != "set":
        return None
    result, i = None, 1
    while i < len(words):
        val, expandable, quoted = words[i]
        if expandable:
            result = "unknown"
            break
        if val in ("--", "-"):
            break
        if not val or val[0] not in "-+":
            break
        if len(val) > 1 and not val.startswith("--") and "o" in val[1:]:
            i += 1
            if i < len(words):
                vval, vexpandable, vquoted = words[i]
                if vexpandable:
                    result = "unknown"
                elif vval == "pipefail":
                    result = val[0] == "-"
                elif vval not in _SET_O_NAMES:
                    break  # unrecognized name -- Bash aborts here
                i += 1
            continue
        i += 1
    return result


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
    next line's command under that guard, and backslash-newline joins.

    A `set` line reporting `"fatal"` (a delimiter-less heredoc, round 22) is
    a real Bash PARSE error: once seen, nothing anywhere later in the WHOLE
    program is reachable, not merely the rest of its own AND-OR list -- so
    it latches a sticky flag that blocks every later append to `out`.

    An undecidable cmd1 still pins the compound's status when the OTHER
    side is a decisive literal: `<unknown> && false` is false either way
    (if cmd1 fails the chain is already false; if it succeeds, `false`
    runs and IS false), and symmetrically `<unknown> || true` is true
    either way -- so the segment after it is credited even though cmd1's
    own status never resolved (round 27, keweichen: `printf if && false
    || python3 live.py` really runs live.py on real Bash 3.2.57/5.2.32,
    confirmed by direct execution). This is narrower than crediting on ANY
    undecidable cmd1: `<unknown> && true` still equals cmd1's own status,
    so `printf if && true && python3 x.py` stays uncredited on purpose --
    declined even though named alongside the `&& false` case, since
    crediting it would regress the `true`/`false`-LHS-only contract this
    file already tests (`python3 real.py && python3 x.py` must still drop
    x.py)."""
    # Named above: &&/|| gate on pending_op/chain_status; the open pipe on
    # last_runs/pipe_group/pipe_entered; pipefail_on is the sticky `set` toggle.
    out, cur, quote, i = [], [], None, 0
    pending_op = None
    chain_status = None
    last_runs = True
    pipe_group = []
    pipe_entered = True
    pipe_negate = False
    pipefail_on = False
    fatal = False

    def transition(sep):
        nonlocal cur, chain_status, pending_op, last_runs
        nonlocal pipe_group, pipe_entered, pipe_negate, pipefail_on, fatal

        _FLIP = {"true": "false", "false": "true", "unknown": "unknown"}

        def pipe_status():
            """The pipe's own resulting status, given tri-state `pipefail_on`
            ("unknown" = a `set ...pipefail` MIGHT have run -- undecidable
            reachability propagates to undecidable pipefail, never to a
            silently-kept old value, keweichen round 12). When the on- and
            off-pipefail answers happen to agree, that agreement still counts.
            A leading `!` negates the WHOLE pipeline's result, not any one
            stage's reachability -- everything still runs the same."""
            if not pipe_group:
                return "unknown"
            off = pipe_group[-1]
            if "false" in pipe_group:
                on = "false"
            elif all(x == "true" for x in pipe_group):
                on = "true"
            else:
                on = "unknown"
            if pipefail_on is True:
                status = on
            elif pipefail_on is False:
                status = off
            else:
                status = off if off == on else "unknown"
            return _FLIP[status] if pipe_negate else status

        s = "".join(cur)
        cur = []
        text = s.strip()
        if text:
            if pending_op != "|" and (text == "!" or text[:2] in ("! ", "!\t")):
                pipe_negate = True
                text = text[1:].strip()
            elif pending_op != "|":
                pipe_negate = False
            if pending_op is None:
                runs, maybe = True, False
            elif pending_op == "|":
                runs, maybe = last_runs, False
            else:
                want = "true" if pending_op == "&&" else "false"
                runs = chain_status == want
                maybe = not runs and chain_status == "unknown"
            # A pipe stage or a backgrounded job both run in a SUBSHELL, so
            # `set` inside either never reaches the parent shell (round 24).
            pipe_scoped = pending_op == "|" or sep == "|" or sep == "&"
            pf = _sets_pipefail(text)
            if pf == "fatal":
                # A real Bash parse error: this segment never actually runs
                # either, and nothing later in the program does (round 22).
                fatal = True
            elif runs and not fatal:
                out.append(s)
            if pf is not None and pf != "fatal" and not pipe_scoped:
                if runs:
                    pipefail_on = pf
                elif maybe:
                    pipefail_on = "unknown"
            last_runs = runs
            shape = text if text in ("true", "false") else "unknown"
            if pending_op == "|":
                pipe_group.append(shape)
            else:
                pipe_group = [shape]
                pipe_entered = runs
        if sep == "|":
            pending_op = "|"
            return
        own_status = pipe_status()
        if pipe_entered:
            chain_status = own_status
        elif pending_op == "&&" and own_status == "false":
            # `<unknown> && false` is false either way (round 27, keweichen).
            chain_status = "false"
        elif pending_op == "||" and own_status == "true":
            chain_status = "true"
        # else: chain_status (the gate's own already-known status) passes
        # through unchanged.
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
        if ch == "|" and nxt == "&":
            # `|&` is `2>&1 |` -- same reachability as a lone `|`, confirmed
            # on Bash 5.2.32 (keweichen); this host's 3.2.57 can't run it.
            transition("|"); i += 2; continue
        if ch in ";|&":
            transition(ch); i += 1; continue
        cur.append(ch); i += 1
    transition(None)
    return [s for s in out if s.strip()]
