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


# A candidate collection is a SEQUENCE the code will actually try in order —
# an array/list literal, or a parenthesised sequence. Stated positively rather
# than as a growing list of exclusions, because each round of "reject the shape
# just reported" left another container that was never a candidate list either:
# a call's argument list, then a keyword grouping, then an object literal.
#
# `in` is the only keyword that introduces a SEQUENCE context (`for x in (...)`).
# `if`/`while`/`return`/`and`/`or`/... introduce a CONDITION or an expression, and
# two same-basename strings inside one are not a fallback chain.
_SEQUENCE_KEYWORDS = frozenset(("in",))

# Words that may legitimately sit before an ARRAY LITERAL. Anything else that
# looks like an identifier is a variable being subscripted.
_LITERAL_PRECEDING_KEYWORDS = frozenset((
    "return", "yield", "and", "or", "not", "in", "is", "if", "else", "elif",
    "while", "await", "lambda", "del", "assert", "case", "match", "for",
    "typeof", "instanceof", "of", "new", "void", "delete",
))


def _prev_word(code, i):
    """(char, word) immediately before `code[i]`, skipping spaces/tabs."""
    j = i - 1
    while j >= 0 and code[j] in " \t":
        j -= 1
    if j < 0:
        return "", ""
    if not (code[j].isalnum() or code[j] == "_"):
        return code[j], ""
    k = j
    while k >= 0 and (code[k].isalnum() or code[k] == "_"):
        k -= 1
    return code[j], code[k + 1:j + 1]


# Suffixes whose `( ... )` is a real SEQUENCE (a Python tuple). In JS/TS the same
# syntax is the COMMA OPERATOR, whose value is only the LAST operand — so
# `("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")` runs the Homebrew path
# and the /usr/local string is dead. Treating that as a candidate list exempted a
# genuinely Apple-Silicon-only command.
_TUPLE_LANG_SUFFIXES = (".py", ".pyi")


# Methods that RESOLVE a candidate list at runtime by probing. Anything else that
# consumes the list selects deterministically at author time (`.at(1)`) and is
# therefore not a fallback chain. Allowlisted, not denylisted: an unknown method
# fails CLOSED.
_RESOLVER_METHODS = frozenset((
    "find", "filter", "some", "includes", "indexOf", "findIndex", "map",
    "flatMap", "reduce", "forEach", "next",
))


def _is_selected_from(code, close_idx, next_code=None):
    """True when the group closing at `close_idx` is SELECTED FROM rather than tried.

        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"][1];
        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].at(1);
        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
        [1];

    All three pick one operand at AUTHOR time, so the other string is dead.

    Three consumers count:
      * a subscript on the same line;
      * a method that is NOT a runtime resolver (`.at`, `.pop`, ...) — the
        allowlist is `_RESOLVER_METHODS`, so an unrecognised method fails CLOSED;
      * a subscript that opens the NEXT line, because JS lets the member
        expression continue across the break. `next_code` carries it.

    `.find(exists)` / `.filter(...)` stay permitted: they choose at RUNTIME by
    probing, which is exactly what makes a candidate list portable.
    """
    j = close_idx + 1
    while j < len(code) and code[j] in " \t":
        j += 1
    if j < len(code):
        if code[j] == "[":
            return True
        if code[j] == ".":
            k = j + 1
            while k < len(code) and (code[k].isalnum() or code[k] == "_"):
                k += 1
            return code[j + 1:k] not in _RESOLVER_METHODS
        return False
    # Group ended at end-of-line: a subscript may open the next line.
    if next_code is not None and next_code.lstrip().startswith("["):
        return True
    return False


def _is_candidate_container(code, i, path=None, prev_code=None):
    """Is `code[i]` the opener of a syntactic candidate COLLECTION?

    * `[` — an array/list literal. An INDEX is excluded, and the discriminator is
      ADJACENCY, not "is there a preceding word": `paths[i]` indexes, while
      `return [...]`, `yield [...]` and `cond and [...]` are literals. An earlier
      version tested only for a preceding word and so false-flagged all three,
      which is ordinary code.
    * `(` — a sequence-keyword grouping (`for _p in (...)`) in any language, or a
      bare tuple (`C = (...)`) ONLY in a language where that is a sequence. Not a
      call's argument list, not a keyword grouping like `if (...)`.
    * `{` — never. An object literal is keyed config; a `fallbackHint` key beside a
      `command` key does not make the command portable.
    """
    ch = code[i]
    if ch == "{":
        return False
    if ch == "[":
        # An INDEX vs an ARRAY LITERAL is decided by what PRECEDES the bracket,
        # and adjacency alone cannot tell them apart: both languages allow
        # whitespace before an index (`paths ["k"]`) and JS has optional element
        # access (`paths?.["k"]`). Conversely `return [...]`, `yield [...]` and
        # `cond and [...]` are literals despite a preceding word.
        #
        # The real discriminator is KEYWORD vs IDENTIFIER:
        #   ?.  )  ]  or an identifier  -> a subscript on an expression
        #   a keyword, an operator, or start-of-line -> a literal
        k = i - 1
        while k >= 0 and code[k] in " \t":
            k -= 1
        if k < 0:
            # Start of line. In JS a member expression may CONTINUE across the
            # break, so `paths\n["k"]` is still an index — the bracket only looks
            # standalone. Consult the previous added line: if it ends in something
            # a subscript can attach to, this is a continuation, not a literal.
            if prev_code is not None:
                tail = prev_code.rstrip()
                if tail and (tail[-1].isalnum() or tail[-1] in "_)]"):
                    return False
            return True                       # genuinely standalone -> literal
        if code[k] == "." and k >= 1 and code[k - 1] == "?":
            return False                      # optional element access
        if code[k] in ")]":
            return False                      # subscript on an expression
        if code[k].isalnum() or code[k] == "_":
            m = k
            while m >= 0 and (code[m].isalnum() or code[m] == "_"):
                m -= 1
            return code[m + 1:k + 1] in _LITERAL_PRECEDING_KEYWORDS
        return True                           # operator / comma / `=` -> literal
    if ch != "(":
        return False
    prev_ch, prev_word = _prev_word(code, i)
    if prev_ch and prev_ch in ")]":
        return False                      # call on a returned value
    if prev_word:
        return prev_word in _SEQUENCE_KEYWORDS
    # Operator / comma / start-of-expression: a tuple in Python, the comma
    # operator in JS/TS. Only the former is a list of alternatives.
    return bool(path) and str(path).endswith(_TUPLE_LANG_SUFFIXES)


def _group_span(code, pos, path=None, prev_code=None, next_code=None):
    """The innermost bracket group containing `pos`, as (start, end), or None.

    Only a syntactic candidate COLLECTION counts as a container (see
    `_is_candidate_container`): an array literal, a tuple, or a
    sequence-keyword grouping. A call's argument list, a keyword grouping like
    `if (...)`, an index, and an object literal are all excluded — none of them
    is a list of alternatives the code will try in order.

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
            if not _is_candidate_container(code, start, path, prev_code):
                continue          # not a syntactic candidate collection
            if _is_selected_from(code, i, next_code):
                continue          # the collection is immediately indexed
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


def paired_allowed(tok, line, pos=None, path=None, prev_code=None, next_code=None):
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
    span = _group_span(code, pos, path, prev_code, next_code)
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
    # Executable text of the PREVIOUS added line. A JS member expression can
    # continue across a newline, so `paths\n["k"]` is an index even though the
    # bracket starts its line. Reset per file and per hunk — a gap between hunks
    # means the preceding line is unknown, and unknown must not read as "standalone".
    prev_added = None
    _lines = diff.split("\n")

    def _next_code(i):
        """Executable text of the next line that is part of the NEW file.

        A member expression may continue onto the following line, so a group that
        ends at EOL can still be subscripted. Added AND context lines both exist
        in the new file and both qualify; a `-`/`@@`/`+++` boundary does not.
        """
        if i + 1 >= len(_lines):
            return None
        nxt = _lines[i + 1]
        if nxt.startswith("+") and not nxt.startswith("+++"):
            return _code_part(nxt[1:])
        if nxt.startswith(" "):
            return _code_part(nxt[1:])
        return None

    for _i, raw in enumerate(_lines):
        next_added = _next_code(_i)
        if raw.startswith("+++ "):
            f = raw[4:].split("\t")[0]
            if f.startswith("b/"):
                f = f[2:]
            cur_file = f
            ln = 0
            skip = bool(SKIP.search(f))
            in_doc = False
            prev_added = None
            continue
        if raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                ln = int(m.group(1))
            # A hunk can't be trusted to continue a prior hunk's string state
            # (gaps between hunks); reset so a docstring opened + closed within
            # this hunk is tracked, without carrying stale state across a gap.
            in_doc = False
            prev_added = None
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith(" "):
            # Context lines exist in both old and new files, so they advance the
            # new-file line counter just like additions do — and they move the
            # docstring state (a docstring may open on unchanged context).
            in_doc = _doc_transition(raw[1:], in_doc)
            ln += 1
            # A context line is part of the NEW file too, so it can be the
            # expression a following added bracket subscripts. Not carrying it
            # made the cross-line discriminator work only when BOTH lines were
            # additions — append a bracket line under unchanged code and the
            # gate exempted it.
            prev_added = _code_part(raw[1:])
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
                    if not allowed(tok) and not paired_allowed(tok, line, pos, cur_file, prev_added, next_added):
                        print("%s:%d: hardcoded path (%s): %s" % (cur_file, cur, tok, stripped))
                        hits += 1
                        reported = True
                        break
                    start = pos + len(p)   # advance past this occurrence
                if reported:
                    break                  # one violation per line is enough
            prev_added = _code_part(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
