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
# Patterns that must equal the WHOLE path token rather than appear inside it.
# A full executable path needs this: '/usr/bin/swift' as a substring also
# rejects '/usr/bin/swift-inspect', a separate real binary (its own inode, link
# count 1) — so a substring rule turns the gate into a blocker for legitimate
# platform tools, which is how a check gets disabled (#2474 review).
flags_exact = [p for p in os.environ.get("RC_FLAGS_EXACT", "").split("\n") if p]
allows = [a for a in os.environ.get("RC_ALLOWS", "").split("\n") if a]
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

    The inference is deliberately one-directional: a `*/` appearing before any
    `/*` can only happen if a comment was opened ABOVE the hunk. No closer, or
    an opener first, means "assume code" — so the multiplication bypass
    (`const n = 2` / `  * "…"`, which contains neither delimiter) is still
    scanned.
    """
    for text in lines:
        # Delimiter-aware: a `*/` inside a string literal is data, not a comment
        # close. Without this, `const closer = "*/";` established block state for
        # the whole hunk and masked the executable code before it — a bypass in
        # the SUPPRESSION direction, the reverse of the string-literal caveat on
        # _mask_comments (@john-the-dev, reviewing #2474).
        bare = _blank_string_literals(text)
        if "/*" in bare:
            return False
        # Require the closer to be the first thing on its line — the canonical
        # JSDoc shape (` */`). A mid-line `*/` that survived string-blanking is
        # not evidence a comment was opened ABOVE this hunk.
        if bare.lstrip().startswith("*/"):
            return True
    return False


def _blank_string_literals(text):
    """Replace quoted spans with spaces so delimiters inside them are ignored.

    Single-line only, which is the right trade here: a template literal spanning
    lines could still hide a delimiter, but this function is used ONLY to decide
    whether a hunk began inside a comment, and the paired line-start requirement
    above means a hidden token cannot establish block state on its own.
    """
    out = []
    quote = None
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
    return "".join(out)


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
    ln = 0
    cur_file = ""
    hits = 0
    in_doc = False   # inside a triple-quoted docstring/string block (reset per hunk)
    in_block = False # inside a /* … */ block comment (inferred per hunk)
    all_lines = diff.split("\n")
    for idx, raw in enumerate(all_lines):
        if raw.startswith("+++ "):
            f = raw[4:].split("\t")[0]
            if f.startswith("b/"):
                f = f[2:]
            cur_file = f
            ln = 0
            skip = bool(SKIP.search(f))
            in_doc = False
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
            # Mask block-comment REGIONS so a path is judged by where it sits on
            # the line, not by the line's state before it. `scan` is what the
            # patterns are matched against; `stripped` stays the real source so
            # the reported line is readable.
            scan_line, in_block = _mask_comments(line, in_block)
            stripped = line.lstrip()
            if was_in_doc or stripped.startswith("#") or stripped.startswith("//"):
                continue
            # (pattern, exact) — exact patterns fire only when the extracted
            # token IS the pattern, so a longer sibling filename in the same
            # directory family (swift-inspect vs swift) is untouched.
            for p, exact in [(f, False) for f in flags] + [(f, True) for f in flags_exact]:
                pos = scan_line.find(p)
                if pos < 0:
                    continue
                tok = token_at(scan_line, pos)
                if exact and tok != p:
                    continue
                if not allowed(tok):
                    print("%s:%d: hardcoded path (%s): %s" % (cur_file, cur, tok, stripped))
                    hits += 1
                    break
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
