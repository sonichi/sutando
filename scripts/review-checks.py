#!/usr/bin/env python3
"""Hardcoded-path scanner for review-checks.sh (kept as a sibling file so the
shell runner never embeds a heredoc inside $(), which macOS's bash 3.2
mis-parses). The pattern lists come via env (RC_FLAGS / RC_ALLOWS — small,
newline-separated lists from the guide's checks: block); the unified diff is
read from STDIN, never argv/env, so an ~8MB PR diff can't blow the OS
'Argument list too long' limit and make the scan silently skip (#2281). Prints
one `file:line: hardcoded path (tok): text` per violation to stdout; exit is 0
when the scan runs (with or without hits — the caller decides pass/fail from
whether anything was printed) and non-zero only if the scanner itself crashes,
so the runner can fail closed."""
import os
import re
import sys

flags = [p for p in os.environ.get("RC_FLAGS", "").split("\n") if p]
allows = [a for a in os.environ.get("RC_ALLOWS", "").split("\n") if a]
# Each entry is "TOKEN_PREFIX :: COMPANION": the token is exempt ONLY when the
# same added line also contains COMPANION. Encodes "portable candidate list"
# without exempting a naked architecture-specific literal.
paired = [tuple(x.strip() for x in a.split("::", 1))
          for a in os.environ.get("RC_ALLOW_PAIRED", "").split("\n")
          if a and "::" in a]
DELIMS = set("\"'()" + ", ;=" + chr(96) + chr(9))   # quotes, brackets, backtick, tab, etc.
SKIP = re.compile(r"\.md$|(^|/)tests/|\.test\.|review-checks\.(sh|py)$")


def token_at(s, pos):
    """The path-ish token containing index `pos` — expand to nearest delimiters."""
    left = pos
    while left > 0 and s[left - 1] not in DELIMS:
        left -= 1
    right = pos
    while right < len(s) - 1 and s[right + 1] not in DELIMS:
        right += 1
    return s[left:right + 1]


def _tokens(s):
    """Every delimiter-separated token on the line (same delimiters token_at uses)."""
    out, cur = [], []
    for ch in s:
        if ch in DELIMS:
            if cur:
                out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _code_part(s):
    """The line with any trailing comment removed.

    The paired allow asks "does this line ALSO run the companion path?" — a
    question only executable text can answer. Scanning the raw line let a
    *comment* satisfy it:

        const FFMPEG = "/opt/homebrew/bin/ffmpeg"; // TODO fallback: /usr/local/bin/ffmpeg

    which passed the gate while the executable value was still
    Apple-Silicon-only — precisely the blind spot the pairing rule exists to
    close. Quote-aware so a `#` or `//` inside a string literal (a URL, a
    fragment) is not mistaken for a comment marker.
    """
    q = None
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return s[:i]
        elif ch == "/" and s[i:i + 2] in ("//", "/*"):
            # `/*` as well as `//`: handling only the two line-comment syntaxes
            # left the same bypass open in a third, and a companion promised in
            # `/* … */` is no more executable than one in `//`. Everything from
            # the opener onward is dropped rather than matching a closing `*/` —
            # the question is only "does the CODE run the companion", and text
            # after a same-line `*/` re-opening code is not a shape this rule
            # needs to bless. Erring toward less code is safe here: it can only
            # withhold the exemption, never grant one.
            return s[:i]
        i += 1
    return s


# Keywords that may legitimately precede a grouping paren. Everything else
# immediately before `(` marks it as a CALL's argument list.
_GROUPING_KEYWORDS = frozenset((
    "in", "for", "if", "elif", "else", "while", "return", "and", "or", "not",
    "yield", "assert", "await", "lambda", "case", "match", "del", "is",
))


def _is_call_paren(code, i):
    """True when `code[i] == '('` opens a CALL's argument list.

    An argument list is not a candidate collection. Two arguments that happen to
    share a basename do not make the first one portable:

        spawn("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")

    The first argument IS the command being launched — an Apple-Silicon-only
    path on Intel — and the second is just another argument, not a fallback the
    code will try. A `(` counts as a call when the preceding non-space character
    closes an expression (`)`/`]`) or ends an identifier that is not a keyword,
    so `for _p in (...)` and `x = (...)` stay genuine groupings.
    """
    j = i - 1
    while j >= 0 and code[j] in " \t":
        j -= 1
    if j < 0:
        return False
    if code[j] in ")]":
        return True
    if not (code[j].isalnum() or code[j] == "_"):
        return False
    k = j
    while k >= 0 and (code[k].isalnum() or code[k] == "_"):
        k -= 1
    return code[k + 1:j + 1] not in _GROUPING_KEYWORDS


def _group_span(code, pos):
    """The innermost bracket group containing `pos`, as (start, end), or None.

    A CALL's argument list is not a container (see `_is_call_paren`) — only a
    genuine collection literal or grouping counts.

    Returns None when `pos` sits inside no such container. That is a FAIL-CLOSED
    answer, not a fallback: an earlier version searched the whole line in that
    case, which let a valid list vouch for a bare direct use later on the same
    line —

        const C = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"];
        const DIRECT = "/opt/homebrew/bin/ffmpeg";

    A token that is not inside a candidate container is not part of a candidate
    list, so it has no companion by construction.
    """
    opens, closes = "([{", ")]}"
    stack, best = [], None
    for i, ch in enumerate(code):
        if ch in opens:
            stack.append(i)
        elif ch in closes and stack:
            start = stack.pop()
            if code[start] == "(" and _is_call_paren(code, start):
                continue          # a call's argument list is not a collection
            if start < pos < i and (best is None or start > best[0]):
                best = (start, i)
    return best


def _siblings_only(code, start, end):
    """`code[start+1:end]` with every DEEPER-nested region blanked.

    The companion must be a sibling in the SAME immediate list/tuple, not merely
    somewhere inside a containing group. Without this, an outer call vouches for
    its own argument via a nested list:

        use(["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
            "/opt/homebrew/bin/ffmpeg")

    The direct argument's innermost container is `use(...)`, whose contents
    include the nested list's companion. Blanking depth>0 leaves only true
    siblings. Length is preserved so callers may reason about offsets.
    """
    opens, closes = "([{", ")]}"
    out, depth = [], 0
    for ch in code[start + 1:end]:
        if ch in opens:
            depth += 1
            out.append(" ")
        elif ch in closes:
            depth -= 1
            out.append(" ")
        else:
            out.append(ch if depth == 0 else (" " if ch != "\n" else ch))
    return "".join(out)


def paired_allowed(tok, line, pos=None):
    """Contextual exemption for the portable candidate-list shape.

    `tok` is exempt only when the SAME line carries a companion path for the
    SAME binary — e.g. '/opt/homebrew/bin/ffmpeg' beside '/usr/local/bin/ffmpeg'.

    Matching the companion's basename (not merely the prefix substring) is
    deliberate: a bare `companion in line` test would exempt the naked form
    whenever any unrelated '/usr/local/...' happened to share the line, which
    re-opens the blind spot this rule exists to close. A naked
    `X = "/opt/homebrew/bin/ffmpeg"` has no same-name companion and stays flagged.

    The companion is sought in the line's CODE only (see `_code_part`): a
    promise in a comment is not a fallback.

    It is also sought only within the flagged occurrence's own bracket GROUP,
    not anywhere on the line. Line-wide matching let a valid candidate list
    vouch for an unrelated direct use of the same binary:

        const C = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"];
        spawn("/opt/homebrew/bin/ffmpeg");

    The third token has the same basename, so it reused the list's companion and
    was exempted while the runtime still launched an Apple-Silicon-only path on
    Intel.

    The companion must be a SIBLING in that same immediate group (see
    `_siblings_only`), and an occurrence inside no group at all is never exempt
    (see `_group_span`). Both are fail-closed: a token that is not part of a
    candidate list has no companion by construction.

    `pos` is the occurrence's index in `line`; it defaults to the first
    occurrence so existing two-argument callers keep their behaviour.
    """
    base = tok.rsplit("/", 1)[-1]
    if not base:
        return False
    code = _code_part(line)
    if pos is None:
        pos = line.find(tok)
    if pos < 0 or pos >= len(code):
        return False
    span = _group_span(code, pos)
    if span is None:
        return False          # no candidate container -> not a candidate list
    scope = _siblings_only(code, span[0], span[1])
    for prefix, companion in paired:
        if not tok.startswith(prefix):
            continue
        for other in _tokens(scope):
            if other.startswith(companion) and other.rsplit("/", 1)[-1] == base:
                return True
    return False


def allowed(tok):
    """A path-like allow (starts with / or ~) must match at the token START, so
    a fixture like `/tmp/` cannot mask a real `/Users/tmp/...`; a non-path allow
    (e.g. a domain) matches anywhere in the token."""
    for a in allows:
        if a[:1] in ("/", "~"):
            if tok.startswith(a):
                return True
        elif a in tok:
            return True
    return False


# Opens an exempt span ONLY for a bare string-expression (the shape of a real
# docstring): optional string prefix (r/b/u/f, up to 2) then a triple-quote at
# the very start of the stripped line. An assignment/call that merely carries a
# triple-quoted literal (`COMMAND = """`) does NOT match, so it stays executable
# code and a hardcoded path inside it is still flagged (#2281, Qingyun review —
# the old count-only toggle suppressed ANY multi-line string, a scanner bypass).
_DOCSTRING_OPEN = re.compile(r"^[rbuf]{0,2}('''|" + '"' * 3 + ")", re.IGNORECASE)


def _doc_transition(line, in_doc):
    """Triple-quote (docstring) state AFTER `line`, given the state before it.

    - Inside a docstring: an ODD number of triple-quote delimiters closes it
      (the closing delimiter may sit anywhere on the line).
    - Not inside one: open only for a bona-fide docstring line whose stripped
      content STARTS with a triple-quote (per _DOCSTRING_OPEN) AND has an odd
      delimiter count (even = a same-line open+close one-liner, stay out). This
      is the assignment-string bypass fix: a triple-quoted literal assigned to a
      name no longer exempts a hardcoded path inside it (#2281, Qingyun review).
    """
    quotes = line.count('"""') + line.count("'''")
    if in_doc:
        return quotes % 2 == 0                       # odd → closed
    return bool(_DOCSTRING_OPEN.match(line.lstrip())) and quotes % 2 == 1


def main():
    diff = sys.stdin.read()  # streamed by the runner — see module docstring (#2281)
    skip = False
    ln = 0
    cur_file = ""
    hits = 0
    in_doc = False   # inside a triple-quoted docstring/string block (reset per hunk)
    for raw in diff.split("\n"):
        if raw.startswith("+++ "):
            f = raw[4:].split("\t")[0]
            if f.startswith("b/"):
                f = f[2:]
            cur_file = f
            ln = 0
            skip = bool(SKIP.search(f))
            in_doc = False
            continue
        if raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                ln = int(m.group(1))
            # A hunk can't be trusted to continue a prior hunk's string state
            # (gaps between hunks); reset so a docstring opened + closed within
            # this hunk is tracked, without carrying stale state across a gap.
            in_doc = False
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith(" "):
            # Context lines exist in both old and new files, so they advance the
            # new-file line counter just like additions do — and they move the
            # docstring state (a docstring may open on unchanged context).
            in_doc = _doc_transition(raw[1:], in_doc)
            ln += 1
            continue
        if raw.startswith("+"):
            if skip:
                continue
            line = raw[1:]
            cur = ln
            ln += 1
            # Decide skip from the state at the line's START, then advance it —
            # so a path sitting inside a docstring (documentation, e.g. a comment
            # describing a legacy path a PR is REMOVING) is not flagged, while
            # the opening `"""` line itself is still checked.
            was_in_doc = in_doc
            in_doc = _doc_transition(line, in_doc)
            stripped = line.lstrip()
            if was_in_doc or stripped.startswith("#") or stripped.startswith("//"):
                continue
            # EVERY occurrence of each flag, not just the first. `paired_allowed`
            # is a PARTIAL exemption, so once the first token on a line pairs
            # successfully a first-occurrence-only scan stops looking — and a
            # second, companion-less literal on the same line passes silently.
            # Harmless before this PR (nothing ever exempted an /opt/ token, so
            # the first occurrence always flagged); a real hole once partial
            # exemptions exist.
            reported = False
            for p in flags:
                start = 0
                while True:
                    pos = line.find(p, start)
                    if pos < 0:
                        break
                    tok = token_at(line, pos)
                    if not allowed(tok) and not paired_allowed(tok, line, pos):
                        print("%s:%d: hardcoded path (%s): %s" % (cur_file, cur, tok, stripped))
                        hits += 1
                        reported = True
                        break
                    start = pos + len(p)   # advance past this occurrence
                if reported:
                    break                  # one violation per line is enough
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
