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


def invokes(line: str, name: str) -> bool:
    """True when `name` runs in COMMAND position on this line.

    Position, not presence: `echo "x.sh"` has the name as an ARGUMENT, an
    assignment `T=x.sh` runs nothing, and `not-x.sh` merely ends with it."""
    import shlex
    code = _strip_comment(line)
    for seg in _segments(code):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        changed = True
        while changed and toks:      # env and VAR=x interleave; peel until neither
            changed = False
            if ("=" in toks[0]) and not toks[0].startswith("="):
                toks = toks[1:]; changed = True
            elif toks[0] == "env" and len(toks) > 1:
                toks = toks[1:]; changed = True
        if toks and toks[0] in ("bash", "sh", "source", ".") and len(toks) > 1:
            toks = toks[1:]
        if toks and toks[0].split("/")[-1] == name:
            return True
    return False


def _segments(line: str):
    """Split on UNESCAPED command separators, so `echo a\\; b` is one command."""
    out, cur, quote, i = [], [], None, 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(ch); cur.append(line[i + 1]); i += 2; continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch; cur.append(ch)
        elif ch in ";|&":
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s for s in out if s.strip()]
