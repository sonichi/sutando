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
import fnmatch
import os
import re
import sys

flags = [p for p in os.environ.get("RC_FLAGS", "").split("\n") if p]
# Patterns that must equal the WHOLE path token rather than appear inside it.
# A full executable path needs this: '/usr/bin/swift' as a substring also
# rejects '/usr/bin/swift-inspect', a separate real binary (its own inode, link
# count 1) — so a substring rule turns the gate into a blocker for legitimate
# platform tools, which is how a check gets disabled (#2474 review).
flags_exact = [p for p in os.environ.get("RC_FLAGS_EXACT", "").split("\n") if p]
allows = [a for a in os.environ.get("RC_ALLOWS", "").split("\n") if a]
# Each entry is "TOKEN_PREFIX :: COMPANION": the token is exempt ONLY when the
# same added line also contains COMPANION. Encodes "portable candidate list"
# without exempting a naked architecture-specific literal.
paired = [tuple(x.strip() for x in a.split("::", 1))
          for a in os.environ.get("RC_ALLOW_PAIRED", "").split("\n")
          if a and "::" in a]
# checks.hardcoded-paths.skip_glob: exempts only a matched file's inner-removal
# lines (see '+' handling below) — an inner addition stays in scope.
skip_globs = [g for g in os.environ.get("RC_SKIP_GLOB", "").split("\n") if g]
DELIMS = set("\"'()" + ", ;=" + chr(96) + chr(9))   # quotes, brackets, backtick, tab, etc.
SKIP = re.compile(r"\.md$|(^|/)tests/|\.test\.|review-checks\.(sh|py)$")


def _skip_file(f):
    """File-class exemption: never scanned at all."""
    return bool(SKIP.search(f))


def _patch_file(f):
    """skip_glob match: only its INNER-REMOVAL lines are exempt, not the file."""
    return any(fnmatch.fnmatchcase(f, g) for g in skip_globs)


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


# Methods that RESOLVE a candidate list at runtime, by narrowing it with a
# predicate the runtime evaluates. Their RESULT is runtime-determined, so a
# subscript on it still is: `.filter(exists)[0]` is a genuine probe. The chain
# ends here.
_PROBING_METHODS = frozenset((
    "find", "filter", "some", "includes", "indexOf", "findIndex", "next",
))

# Methods that pass the list THROUGH without narrowing it by a runtime predicate.
# They neither resolve nor select, so reading them as resolvers and stopping let a
# deterministic selector hide one link further down the chain:
#
#     ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => x)[1]
#
# `map` is 1:1, so `[1]` is the same AUTHOR-time pick as `[1]` on the literal —
# the /usr/local string is present but dead. Keep walking instead.
_TRANSFORM_METHODS = frozenset(("map", "flatMap", "reduce", "forEach"))


# A `/` after one of these keywords opens a REGEX: each takes an operand, so the
# identifier before the slash does not end an expression.
_REGEX_KEYWORDS = frozenset((
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "case", "do", "else", "yield", "await", "throw",
))


# Keywords after which a `{` opens a BLOCK even though an operand is expected.
_STMT_KEYWORDS = frozenset(("else", "do", "try", "finally"))


def _lex(s, probe=None):
    """One left-to-right lex of a single line. Returns (blanked, probe_answer).

    `blanked` is `s` with the CONTENTS of every string, template and regex
    literal replaced by spaces — same length, delimiters kept, so indices stay
    comparable with the original. Bracket counting must not see punctuation that
    is data; a quoted or regex-escaped `)` used to close a call early, which let
    `_call_end` stop inside a callback and miss a deterministic subscript.

    `probe` is an index; `probe_answer` is True when a `/` there opens a REGEX
    rather than divides.

    Everything hangs off ONE piece of state: is an OPERAND expected at this
    point? That single question answers all three ambiguous tokens, which is why
    this replaced a growing list of per-token special cases —

      *  `/`  opens a regex when an operand is expected, else it divides;
      *  `{`  is an object LITERAL when an operand is expected, else a BLOCK;
      *  `}`  ends an object (an expression, so a `/` after it divides) or ends
              a block (a statement, so a `/` after it opens a regex) — which one
              is known from the brace stack, not from the character.

    That last pair is the reason for the stack. Reading `}` as always-division
    let `if (x) {} /\\)/.test(x)` hide a `)` inside regex data; reading it as
    always-regex blanked `{} / 2` and flagged valid code. Neither answer is
    right for the character alone; both are right for the brace it closes.

    Two positions need help beyond the operand flag, because a `{` there opens a
    BLOCK even though an operand is expected: an arrow body (`x => {`) and the
    statement keywords (`else {`, `do {`, `try {`, `finally {`).
    """
    out = list(s)
    n = len(s)
    operand = True        # an operand/expression is expected here
    block_next = False    # the next `{` is a block body, not an object literal
    braces = []           # True = object literal, False = statement block
    ans = None
    i = 0
    while i < n:
        ch = s[i]
        if ch in " \t":
            i += 1
            continue
        if ch in "\"'`":                       # string / template literal
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    out[j] = " "
                    if j + 1 < n:
                        out[j + 1] = " "
                    j += 2
                    continue
                if s[j] == ch:
                    break
                out[j] = " "
                j += 1
            operand, block_next, i = False, False, j + 1
            continue
        if ch == "/":
            if probe == i:
                ans = operand
            if not operand:                     # division
                operand, block_next, i = True, False, i + 1
                continue
            j = i + 1                           # regex literal
            cls = False
            while j < n:
                if s[j] == "\\":
                    out[j] = " "
                    if j + 1 < n:
                        out[j + 1] = " "
                    j += 2
                    continue
                if s[j] == "[":
                    cls = True
                elif s[j] == "]":
                    cls = False
                elif s[j] == "/" and not cls:
                    break
                out[j] = " "
                j += 1
            operand, block_next, i = False, False, j + 1
            continue
        if ch == "{":
            braces.append(operand and not block_next)
            operand, block_next, i = True, False, i + 1
            continue
        if ch == "}":
            # Closing an object literal ends an EXPRESSION; closing a block ends
            # a STATEMENT. An empty stack means the `{` is off-line: assume the
            # expression reading, which is what a bare `{...} / 2` fragment is.
            operand = not (braces.pop() if braces else True)
            block_next, i = False, i + 1
            continue
        if ch in "([":
            operand, block_next, i = True, False, i + 1
            continue
        if ch in ")]":
            operand, block_next, i = False, False, i + 1
            continue
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (s[j].isalnum() or s[j] in "_$"):
                j += 1
            word = s[i:j]
            operand = word in _REGEX_KEYWORDS or word in _STMT_KEYWORDS
            block_next = word in _STMT_KEYWORDS
            i = j
            continue
        if s[i:i + 2] in ("++", "--"):
            # Postfix keeps the expression closed; prefix keeps an operand due.
            i += 2
            continue
        if s[i:i + 2] == "=>":
            operand, block_next, i = True, True, i + 2
            continue
        operand, block_next, i = True, False, i + 1   # any other operator
    return "".join(out), ans


def _blank_strings(s):
    """`s` with the contents of every string, template and regex literal blanked."""
    return _lex(s)[0]


def _starts_regex(s, i):
    """Does the `/` at `s[i]` open a regex literal rather than divide?"""
    ans = _lex(s, probe=i)[1]
    return True if ans is None else ans


def _call_end(code, name_end):
    """Index of the `)` closing the call whose name ends at `name_end`, else None.

    Counts on `_blank_strings(code)` so a `)` inside a callback's string literal
    is data, not syntax.

    None means the shape could not be read (no call parens, or unbalanced because
    the diff truncated the line). Callers treat that as UNKNOWN and fail closed —
    an unreadable chain must not read as a resolved one.
    """
    code = _blank_strings(code)
    j = name_end
    while j < len(code) and code[j] in " \t":
        j += 1
    if j >= len(code) or code[j] != "(":
        return None
    depth = 0
    for k in range(j, len(code)):
        if code[k] == "(":
            depth += 1
        elif code[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


# A chain is carried until it RESOLVES or TERMINATES. `_CHAIN_LOOKAHEAD` bounds
# only the WORK — how many non-blank continuation lines are read per token — so a
# large hunk is not rescanned endlessly. Blank lines cost nothing and do not spend
# it, and running out does NOT mean "not selected": the walk emits
# `_CHAIN_TRUNCATED` and fails CLOSED.
#
# That direction is the whole point. A permissive bound is defeatable by writing
# bound+1 lines, so every value of it is wrong; a conservative one cannot be,
# because exhausting it flags. Same rule as an unreadable call chain.
_CHAIN_LOOKAHEAD = 24
_CHAIN_TRUNCATED = "\x00truncated"


def _as_lines(next_code):
    """Normalise `next_code` to a tuple of following new-file lines.

    Accepts a single string (one lookahead line, the shape the unit tests use)
    or a tuple of them. `None` is no lookahead at all.
    """
    if next_code is None:
        return ()
    if isinstance(next_code, str):
        return (next_code,)
    return tuple(next_code)


def _is_selected_from(code, close_idx, next_code=None):
    """True when the group closing at `close_idx` is SELECTED FROM rather than tried.

        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"][1];
        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].at(1);
        const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
        [1];

    All three pick one operand at AUTHOR time, so the other string is dead.

    Three consumers count:
      * a subscript on the same line;
      * a method that is NOT a runtime probe (`.at`, `.pop`, ...) — the allowlist
        is `_PROBING_METHODS`, so an unrecognised method fails CLOSED;
      * a subscript OR a method that opens a FOLLOWING line, because JS lets the
        member expression continue across a break. `next_code` carries every
        remaining new-file line, and the walk consumes them until the chain
        RESOLVES (a probe) or TERMINATES (a selector, or something that is
        neither) — not for a fixed number of lines. Bounding it at one physical
        line meant ordinary formatting could put the selector on a third line
        and slip past.

    `.find(exists)` / `.filter(...)` stay permitted: they choose at RUNTIME by
    probing, which is exactly what makes a candidate list portable.

    A pass-through transform (`_TRANSFORM_METHODS`) is neither: it does not
    resolve the list, so the chain is READ ON past it rather than accepted. That
    is what stops `.map(x => x)[1]` — deterministic selection one link further
    down — while `.map(x => x).find(exists)` still resolves at runtime.
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
            name = code[j + 1:k]
            if name in _PROBING_METHODS:
                return False
            if name in _TRANSFORM_METHODS:
                end = _call_end(code, k)
                if end is None:
                    return True       # unreadable chain — fail closed
                return _is_selected_from(code, end, next_code)
            return True
        return False
    # Group ended at end-of-line: the member expression may continue below.
    for k, follow in enumerate(_as_lines(next_code)):
        nxt = follow.lstrip()
        if not nxt:
            continue                      # a blank line does not end a chain
        rest = tuple(_as_lines(next_code)[k + 1:])
        if nxt.startswith("["):
            return True
        if follow is _CHAIN_TRUNCATED:
            return True                   # unresolved at the budget — fail CLOSED
        if nxt.startswith("."):
            # Re-enter with a synthetic close at index 0 so exactly the same
            # rules apply as on one line, carrying the REMAINING lines so a
            # selector further down is still reached.
            return _is_selected_from("]" + nxt, 0, rest)
        return False                      # not a continuation — the chain ended
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


def _block_transition(line, in_block):
    """Block-comment (`/* … */`) state AFTER `line`, given the state before it.

    STATEFUL on purpose. A first attempt skipped any line whose stripped form
    began with `* `, on the theory that only JSDoc bodies look like that. It is
    also valid JavaScript for a continued multiplication:

        const n = 2
          * "/usr/bin/python3".length;

    …so the second line was treated as prose and the path went unflagged — a
    scanner bypass, not a false negative on prose (@john-the-dev, reviewing
    #2474). Tracking the real `/*` … `*/` span means a line is exempt because
    it IS inside a comment, never because of how it happens to start.

    A one-liner `/* … */ code` opens and closes within the line, so it ends
    OUTSIDE the block and the code on it is still scanned — a comment cannot
    smuggle a path onto a code line.

    Known limitation, shared with the docstring tracker above: a `/*` or `*/`
    inside a string literal moves the state. Erring toward scanning is the safe
    direction, and the flagged-path cost of the reverse is what this fixes.
    """
    return _mask_comments(line, in_block)[1]


def _mask_comments(line, in_block):
    """Blank out block-comment REGIONS of `line`; return (masked, state_after).

    Position-level rather than line-level, because both halves of a line matter:

        /** helper for /usr/bin/python3 resolution     ← path is inside the
                                                         comment it opens: exempt
        /* note */ const p = "/usr/bin/git";           ← path is after the close:
                                                         still scanned

    A line-level `was_in_block` test gets the first case wrong (the comment is
    entered ON this line, so the state BEFORE it is False) — which flagged
    legitimate resolver documentation (@john-the-dev, reviewing #2474).

    Masking with spaces rather than deleting keeps every column index stable, so
    the reported token and line number still line up with the real source.
    """
    out = []
    i = 0
    n = len(line)
    state = in_block
    while i < n:
        if not state and line.startswith("/*", i):
            state = True
            out.append("  ")
            i += 2
            continue
        if state and line.startswith("*/", i):
            state = False
            out.append("  ")
            i += 2
            continue
        out.append(" " if state else line[i])
        i += 1
    return "".join(out), state


def _hunk_opens_in_block(lines):
    """True when a hunk's own content proves it began INSIDE a block comment.

    A unified diff shows three lines of context, so editing a JSDoc body more
    than three lines below its `/**` leaves the opener outside the hunk
    entirely. Resetting block state at every `@@` therefore scanned ordinary
    documentation as code — the single most likely way this gate would start
    rejecting legitimate resolver docs (@john-the-dev, reviewing #2474).

    The inference requires CORROBORATION, not just a closer. A line-start `*/`
    alone is not proof: it is reachable from ordinary code via a multi-line
    template literal, whose opening backtick the single-line
    `_blank_string_literals` cannot carry across lines —

        const p = "/usr/bin/python3"; const tpl = `
        */
        `;

    — which let a hidden closer start the whole hunk in block state and mask the
    executable stub path on line 1 (@john-the-dev, reviewing #2474; an earlier
    revision of this docstring claimed the line-start rule prevented exactly
    that, and this control disproved it).

    So a closer only counts when some EARLIER line in the hunk already looks
    like comment body (`*` or `/*` leading). Real JSDoc always has that; the
    template literal does not. Everything else means "assume code" — so the
    multiplication bypass (`const n = 2` / `  * "…"`, which has no closer at
    all) is still scanned.
    """
    saw_comment_body = False
    quote = None
    for text in lines:
        # Delimiter-aware: a `*/` inside a string literal is data, not a comment
        # close. Without this, `const closer = "*/";` established block state for
        # the whole hunk and masked the executable code before it — a bypass in
        # the SUPPRESSION direction, the reverse of the string-literal caveat on
        # _mask_comments (@john-the-dev, reviewing #2474).
        bare, quote = _blank_string_literals(text, quote)
        if "/*" in bare:
            return False
        # Require the closer to be the first thing on its line — the canonical
        # JSDoc shape (` */`). A mid-line `*/` that survived string-blanking is
        # not evidence a comment was opened ABOVE this hunk.
        stripped = bare.lstrip()
        if stripped.startswith("*/"):
            return saw_comment_body
        if stripped.startswith("*"):
            saw_comment_body = True
    return False


def _blank_string_literals(text, quote=None):
    """Blank quoted spans so delimiters inside them are ignored.

    Returns (blanked, quote_after). `quote_after` is a BACKTICK or None: a
    template literal is the only JS string that survives a newline, so it is the
    only state worth carrying to the next line. An unterminated ' or " is a
    single-line syntax error, not state.

    Carrying that state is what closes the corroboration bypass. Both the
    evidence line AND the closer can sit inside one multiline template:

        const p = "/usr/bin/python3"; const tpl = `
        * template content
        */
        `;

    which is valid JS. Line-at-a-time blanking saw `* template content` as
    comment-body evidence and `*/` as a closer, inferred the hunk had opened
    inside a comment, and masked the executable path on line 1
    (@john-the-dev, reviewing #2474). With the backtick carried, lines 2-3 are
    blanked as string content and provide no evidence at all.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                out.append("  ")
                i += 2
                continue
            out.append(" ")
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    # Only a template literal survives to the next line.
    return "".join(out), (quote if quote == "`" else None)


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


def _hunk_body(all_lines, start):
    """New-file view of the hunk beginning after index `start`: context + added
    lines with their diff marker stripped, stopping at the next hunk or file."""
    body = []
    j = start + 1
    while j < len(all_lines):
        nxt = all_lines[j]
        if nxt.startswith("@@ ") or nxt.startswith("+++ ") or nxt.startswith("diff "):
            break
        if nxt.startswith(" ") or nxt.startswith("+"):
            body.append(nxt[1:])
        j += 1
    return body


def main():
    diff = sys.stdin.read()  # streamed by the runner — see module docstring (#2281)
    skip = False
    is_patch = False
    ln = 0
    cur_file = ""
    hits = 0
    in_doc = False   # inside a triple-quoted docstring/string block (reset per hunk)
    in_block = False # inside a /* … */ block comment (inferred per hunk)
    # Executable text of the PREVIOUS added line. A JS member expression can
    # continue across a newline, so `paths\n["k"]` is an index even though the
    # bracket starts its line. Reset per file and per hunk — a gap between hunks
    # means the preceding line is unknown, and unknown must not read as "standalone".
    prev_added = None
    all_lines = diff.split("\n")

    # Next-MEANINGFUL-line links, precomputed once in a single backward pass.
    #
    # The walk must be blank-insensitive (formatting must not hide a selector) but
    # scanning the blank suffix per input line is QUADRATIC: a hunk with N added
    # blank lines re-reads and re-materialises nearly the whole suffix N times.
    # Measured before this table existed: 1k blanks 0.14s, 4k 1.64s, 8k 6.14s —
    # 4x input for ~12x time, which lets a whitespace-heavy PR drive a REQUIRED
    # gate toward CI timeout.
    #
    # `_nm[i]` is the first continuation-eligible new-file line AFTER `i` whose
    # executable text is non-blank, or None when a hunk boundary intervenes.
    # Building it is O(lines); following it is O(_CHAIN_LOOKAHEAD) per token, so
    # blanks cost nothing at either end.
    _codes = [None] * len(all_lines)
    _nm = [None] * len(all_lines)
    _carry = None
    for _k in range(len(all_lines) - 1, -1, -1):
        _l = all_lines[_k]
        if _l.startswith("+++") or not (_l.startswith("+") or _l.startswith(" ")):
            _carry = None                 # boundary: nothing past it is reachable
            continue
        _codes[_k] = _code_part(_l[1:])
        _nm[_k] = _carry
        if _codes[_k].strip():
            _carry = _k

    def _next_code(i):
        """The next meaningful new-file lines after `i`, following `_nm`.

        Blank lines are skipped by the links rather than collected, so the tuple
        holds at most `_CHAIN_LOOKAHEAD` entries however much whitespace sits in
        between. A chain still open at the cap gets `_CHAIN_TRUNCATED` appended so
        the walk fails CLOSED.
        """
        out = []
        j = _nm[i] if i < len(_nm) else None
        while j is not None:
            if len(out) == _CHAIN_LOOKAHEAD:
                out.append(_CHAIN_TRUNCATED)
                break
            out.append(_codes[j])
            j = _nm[j]
        return tuple(out) or None

    for idx, raw in enumerate(all_lines):
        next_added = _next_code(idx)
        if raw.startswith("+++ "):
            f = raw[4:].split("\t")[0]
            if f.startswith("b/"):
                f = f[2:]
            cur_file = f
            ln = 0
            skip = _skip_file(f)
            is_patch = _patch_file(f)
            in_doc = False
            prev_added = None
            in_block = False
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
            # Block state cannot simply reset: the `/**` opener is routinely
            # outside the 3 lines of context. Infer it from the hunk's own
            # content instead (see _hunk_opens_in_block).
            in_block = _hunk_opens_in_block(_hunk_body(all_lines, idx))
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith(" "):
            # Context lines exist in both old and new files, so they advance the
            # new-file line counter just like additions do — and they move the
            # docstring state (a docstring may open on unchanged context).
            in_doc = _doc_transition(raw[1:], in_doc)
            in_block = _block_transition(raw[1:], in_block)
            ln += 1
            # A context line is part of the NEW file too, so it can be the
            # expression a following added bracket subscripts. Not carrying it
            # made the cross-line discriminator work only when BOTH lines were
            # additions — append a bracket line under unchanged code and the
            # gate exempted it.
            prev_added = _code_part(raw[1:])
            continue
        if raw.startswith("+"):
            line = raw[1:]
            if skip:
                continue
            # A skip_glob file is exempt ONLY on its nested diff's own removal
            # lines; an inner addition is what a re-applied patch introduces.
            if is_patch and line.startswith("-"):
                continue
            cur = ln
            ln += 1
            # Decide skip from the state at the line's START, then advance it —
            # so a path sitting inside a docstring (documentation, e.g. a comment
            # describing a legacy path a PR is REMOVING) is not flagged, while
            # the opening `"""` line itself is still checked.
            was_in_doc = in_doc
            in_doc = _doc_transition(line, in_doc)
            # Mask block-comment REGIONS so a path is judged by where it sits on
            # the line, not by the line's state before it. `scan` is what the
            # patterns are matched against; `stripped` stays the real source so
            # the reported line is readable.
            scan_line, in_block = _mask_comments(line, in_block)
            stripped = line.lstrip()
            if was_in_doc or stripped.startswith("#") or stripped.startswith("//"):
                continue
            # EVERY occurrence of each flag, not just the first (`paired_allowed`
            # is a PARTIAL exemption — a first-occurrence-only scan would let a
            # second, companion-less literal on the same line pass silently), and
            # (pattern, exact) pairs — exact patterns fire only when the extracted
            # token IS the pattern (swift-inspect vs swift stays untouched).
            # Scans the comment-MASKED text (offset-preserving), pairing context
            # reads the original line.
            reported = False
            for p, exact in [(f, False) for f in flags] + [(f, True) for f in flags_exact]:
                start = 0
                while True:
                    pos = scan_line.find(p, start)
                    if pos < 0:
                        break
                    tok = token_at(scan_line, pos)
                    if exact and tok != p:
                        start = pos + len(p)
                        continue
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
