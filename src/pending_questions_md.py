"""Locating the `# Resolved` divider in pending-questions.md — one definition.

Four readers independently reimplemented this cut (the notifier, morning-briefing,
agent-api and friction-detector), all with `r'^#\\s+Resolved\\b'`, and on
2026-07-30 all four went wrong at once. Two distinct failures, opposite directions:

  * **under-count (host A).** The file's own banner warns writers not to append at
    EOF and *quotes the rule it documents*, putting `` # Resolved` heading ``
    line-initial inside an HTML comment. `\\b` is satisfied by the backtick, so the
    split fired in the banner: the active region collapsed to the banner and all 89
    real sections were counted as resolved. Measured: 0 open while 43 were open.
  * **over-count (host B).** A file with no clean divider: the split is a no-op, so
    the audit trail below is counted as pending. Measured: 101.

The tempting fix — anchor the divider to end-of-line (`^#[ \\t]+Resolved[ \\t\\r]*$`)
— kills the under-count but **regresses a suffixed divider**: `# Resolved (archive)`
stops matching, so its whole audit trail is counted as open. A stricter anchor can
only make the active region larger, so by construction it can never fix an
over-count either. Verified on both.

So the discriminator is not how the divider ENDS, it is **whether the match sits
inside markup**. A real divider is prose-level Markdown; every documentation of the
divider is quoted — in a comment, a fenced block, or an inline code span.

Three maskers blank those regions while preserving BOTH length and line count, so a
caller can search the masked text and slice the ORIGINAL by the same offset. That
matters: agent-api derives question identity from section bodies, and stripping
(rather than masking) would shift offsets and silently change ids.

  * `mask_html_comments`  — `<!-- ... -->`          (host-A under-count)
  * `mask_fenced_code`    — ``` / ~~~ blocks         (#2419 review, 2026-07-31)
  * `_mask_nonfence_spans` — `` `...` ``, may wrap   (live on a third host, same day)

The last two were added after review: a fenced *example* of the divider, and a
sentence that line-wrapped so `` # Resolved` `` landed line-initial inside an inline
code span, each truncated the active region to nothing. Because all five readers now
share this helper, one such decoy hides every later owner question at once — the
exact false-zero class this module exists to remove.

Masking is used ONLY to locate the divider. What counts as a section is deliberately
unchanged — a `## ` heading that lives inside a comment keeps whatever behavior it
had, because that is a separate question from where the audit trail begins.
"""
from __future__ import annotations

import re

# Permissive on the suffix (`# Resolved (archive)` is a real divider) but anchored
# with `[ \t]` rather than `\s`, so the whitespace class cannot span a newline and
# match a bare `#` on one line against `Resolved` on the next.
DIVIDER_RE = re.compile(r'^#[ \t]+Resolved\b', re.MULTILINE)

# friction-detector also treats `# Done` as an archive divider.
DIVIDER_OR_DONE_RE = re.compile(r'^#[ \t]+(?:Resolved|Done)\b', re.MULTILINE)

_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)

# CommonMark fence: 0–3 spaces of indent, then 3+ backticks or 3+ tildes. Four
# spaces makes it an indented code block, not a fence — so `{0,3}` is load-bearing,
# not cosmetic. A backtick opener's info string may not contain a backtick.
_FENCE_OPEN_RE = re.compile(r'^( {0,3})(`{3,}|~{3,})([^\n]*)$')

# Inline code spans are parsed, not regexed. A regex of the form ``(`+)…\1`` can
# backtrack to a PREFIX of a longer run and close on a PREFIX of another, pairing
# runs of unequal length — which exposes a divider that is genuinely quoted. Markdown
# requires both delimiters to be MAXIMAL runs of EXACTLY equal length.


def _blank(s: str) -> str:
    """Replace every character except newlines with a space (length-preserving)."""
    return re.sub(r'[^\n]', ' ', s)


def mask_html_comments(text: str) -> str:
    """Blank the contents of HTML comments, preserving length and line breaks.

    Offsets into the result are valid offsets into `text`, so callers may search
    here and slice there.
    """
    return _COMMENT_RE.sub(lambda m: _blank(m.group(0)), text)


def mask_fenced_code(text: str) -> str:
    """Blank fenced code blocks (``` and ~~~), preserving length and line breaks.

    Closing rules follow CommonMark, because the loose versions are what a decoy
    exploits: a closer must use the SAME marker character and be AT LEAST as long
    as the opener, so a shorter inner run does not close a longer fence. An
    unclosed fence runs to end of document — which masks the real divider too and
    therefore fails toward *over*-counting (visible noise) rather than the silent
    zero this module exists to prevent.
    """
    out, in_fence, char, size = [], False, '', 0
    for line in text.split('\n'):
        if not in_fence:
            m = _FENCE_OPEN_RE.match(line)
            # A backtick opener may not carry a backtick in its info string.
            if m and not (m.group(2)[0] == '`' and '`' in m.group(3)):
                in_fence, char, size = True, m.group(2)[0], len(m.group(2))
                out.append(_blank(line))
            else:
                out.append(line)
        else:
            out.append(_blank(line))
            stripped = line.strip()
            # Indentation is measured on the RAW line with an explicit
            # `^ {0,3}\S`, not by counting leading spaces after `strip()`.
            # `line.strip()` removes a leading TAB, and the separate
            # `lstrip(' ')` count then reported an indent of 0 — two checks
            # disagreeing about what "indentation" means, so `\t```` was
            # accepted as a valid closer and the fence closed early, exposing a
            # quoted `# Resolved` as the real divider (silent zero).
            if (stripped and set(stripped) == {char} and len(stripped) >= size
                    and re.match(r'^ {0,3}\S', line) is not None):
                in_fence = False
    return '\n'.join(out)


def _backtick_runs(text: str) -> list[tuple[int, int]]:
    """Every MAXIMAL run of backticks as (start, length)."""
    runs, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '`':
            j = i
            while j < n and text[j] == '`':
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


def _opens_span(text: str, start: int, length: int) -> bool:
    """Is the backtick run at `start` unambiguously an INLINE span delimiter?

    True only when the run cannot be read as a fence marker for a reason that
    makes it inline:

      * TEXT BEFORE IT on the line  — a fence marker must begin its line;
      * a run shorter than 3        — never a fence;
      * a backtick in its info string — a backtick fence may not carry one.

    Deliberately NOT included: a run alone on its line but indented by a tab or
    4+ spaces. That is an INVALID FENCE MARKER, not an inline delimiter — it is
    ordinary text inside the enclosing fence. Treating it as a span opener let
    it pair with the fence's real closer and blank it, so the fence ran to end
    of document and swallowed the REAL divider along with the quoted one. The
    suite caught that as `## archived` being counted; the fixture's own trailing
    `# Resolved` is what makes the over-mask visible.
    """
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip(" \t") != "":
        return True           # text before it -> inline
    if length < 3:
        return True           # too short to fence
    line_end = text.find("\n", start)
    info = text[start + length:] if line_end < 0 else text[start + length:line_end]
    return "`" in info        # invalid fence info -> inline


def _mask_nonfence_spans(text: str) -> str:
    """Blank inline spans whose OPENER cannot be a fence marker.

    Such an opener is unambiguously a span delimiter, so it may close on the next
    equal-length maximal run even when that run sits at column zero and could
    otherwise have opened a fence — which is exactly G1, where a span opened
    mid-line must reach across an intervening ```` fence.

    Runs that ARE fence-eligible never open a span here; they are left for
    `mask_fenced_code`, which applies the raw 0-3-space closer contract. That is
    what keeps the tab, space+tab, 4-space and trailing-text closers rejected.
    """
    runs = _backtick_runs(text)
    opens = [_opens_span(text, s, n) for s, n in runs]
    out = list(text)
    k = 0
    while k < len(runs):
        if not opens[k]:
            k += 1
            continue
        start, length = runs[k]
        m = k + 1
        while m < len(runs) and runs[m][1] != length:
            m += 1
        if m < len(runs):
            end = runs[m][0] + runs[m][1]
            for q in range(start, end):
                if out[q] != "\n":
                    out[q] = " "
            k = m + 1
        else:
            k += 1
    return "".join(out)


def mask_markup(text: str) -> str:
    """Comments, then non-fence-opened spans, then fences.

    NOT a global precedence ordering — every ordering breaks a real case,
    measured across five designs. The runs are instead PARTITIONED by whether
    each one could be a fence marker at all (`_fence_eligible`):

      * a run with TEXT BEFORE IT, a run shorter than 3, or a backtick in its
        info string can only be an inline delimiter -> resolved first, and
        allowed to close on any equal-length run, crossing fences if need be
        (G1, G2, F7, G5);
      * every other run — including one alone on its line but indented by a tab
        or 4+ spaces — is left to `mask_fenced_code` and its raw 0-3-space
        closer contract, so those closers are rejected and the fence keeps
        running (H1/I1, S1/I2, S2/I3).

    The two classes do not compete, so there is nothing left to arbitrate.

    Every step preserves length and line count, so a caller may search the
    masked text and slice the ORIGINAL at the same offsets.
    """
    return mask_fenced_code(_mask_nonfence_spans(mask_html_comments(text)))


def active_region(text: str, divider: re.Pattern = DIVIDER_RE) -> str:
    """`text` up to the first real (non-quoted) archive divider.

    Returns `text` unchanged when there is no divider — a file that keeps no audit
    trail is entirely active.
    """
    m = divider.search(mask_markup(text))
    return text[:m.start()] if m else text
