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
import sys

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


def mask_fenced_code(text: str, *, report_unclosed: bool = False):
    """Blank fenced code blocks (``` and ~~~), preserving length and line breaks.

    Closing rules follow CommonMark, because the loose versions are what a decoy
    exploits: a closer must use the SAME marker character and be AT LEAST as long
    as the opener, so a shorter inner run does not close a longer fence. An
    unclosed fence runs to end of document — which masks the real divider too and
    therefore fails toward *over*-counting (visible noise) rather than the silent
    zero this module exists to prevent.
    """
    out, in_fence, char, size = [], False, '', 0
    off, open_off = 0, 0
    for line in text.split('\n'):
        if not in_fence:
            m = _FENCE_OPEN_RE.match(line)
            # A backtick opener may not carry a backtick in its info string.
            if m and not (m.group(2)[0] == '`' and '`' in m.group(3)):
                in_fence, char, size = True, m.group(2)[0], len(m.group(2))
                open_off = off
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
        off += len(line) + 1          # +1 for the '\n' split consumed
    masked = '\n'.join(out)
    # Closure is STRUCTURAL state the parser already has. Callers that need to
    # tell a runaway fence from a deliberate example must ask for it here rather
    # than infer it from content — inferring it produced two false alarms
    # (a closed fence at EOF, review of #2558).
    #
    # Reported as the unclosed fence's RANGE, not a bare bool. "Is there an
    # unclosed fence anywhere in this document" cannot answer "did an unclosed
    # fence hide THIS divider": a closed fence quoting `# Resolved` plus an
    # unrelated runaway fence later in the file made the quoted divider warn as
    # damage, on a document where nothing was hidden and nothing archived was
    # served as live (qingyun-wu P1 on 8ad855ac, reproduced here before fixing).
    # A span lets the caller ask the local question. `None` when the document is
    # balanced, so truthiness still reads the way the old bool did.
    span = (open_off, len(text)) if in_fence else None
    return (masked, span) if report_unclosed else masked


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


def _mask_nonfence_spans(text: str, report_ranges: bool = False):
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
    ranges: "list[tuple[int, int]]" = []
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
            ranges.append((start, end))
            k = m + 1
        else:
            k += 1
    masked = "".join(out)
    # `report_ranges` exists because "was a question swallowed" has to be asked
    # about the SPECIFIC span that hid a given divider. Newlines are preserved
    # above, so a masked region is not a contiguous run of changed characters and
    # cannot be recovered by diffing the two strings.
    return (masked, ranges) if report_ranges else masked


def mask_markup(text: str) -> str:
    """Comments, then non-fence-opened spans, then fences.

    NOT a global precedence ordering — every ordering breaks a real case,
    measured across five designs. The runs are instead PARTITIONED by whether
    each one could be a fence marker at all (`_opens_span`):

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




def _is_question_shape(line: str) -> bool:
    """True when a READER would count this line as a question entry."""
    return bool(line.startswith("## ") or re.match(r'^\s*-\s+\*\*\[(.+?)\]', line))


def _question_entry_masked_in(text: str, masked: str, lo: int, hi: int) -> bool:
    """True when the span covering [lo, hi) swallowed a reader-recognised question.

    Scoped deliberately. Asking the question document-globally meant a question
    swallowed by ONE deliberate span counted as damage for a divider hidden by a
    DIFFERENT, unrelated span — two independent balanced quotes in the same file
    warned even though each was intentional and nothing live was lost.
    """
    pos = 0
    for raw_line, masked_line in zip(text.split("\n"), masked.split("\n")):
        start, end = pos, pos + len(raw_line)
        pos = end + 1                                  # +1 for the newline
        if end <= lo or start >= hi:
            continue                                   # outside THIS span
        if raw_line == masked_line:
            continue
        # The RECOGNISED PREFIX must be what disappeared. Testing only "raw looks
        # like a question AND the line changed" fired when a span opened AFTER the
        # `## `, leaving the heading fully visible to every reader while some later
        # part of its line was quoted. Nothing was swallowed; the entry is still
        # readable. So: raw matches the shape, masked no longer does.
        if _is_question_shape(raw_line) and not _is_question_shape(masked_line):
            return True
    return False


def _dividers_hidden_by_damage(text: str, divider: re.Pattern) -> list[tuple[int, str]]:
    """Every divider hidden by DAMAGED markup, as (line, matched text).

    Three review rounds went into this predicate; the corrections are the point.

    **An unpaired backtick run masks NOTHING** (`_mask_nonfence_spans` only blanks a
    run that finds an equal-length partner). So the real-world damage was never
    "unbalanced markup" — it was a span that pairs LEGITIMATELY across ~1,900 lines
    and swallows the divider on the way. That is structurally identical to a
    deliberate two-line quote, so nothing about the span itself separates them.

    What separates them is what the span SWALLOWS. A deliberate quote covers the
    divider it is quoting; runaway markup takes live questions with it. Hence:

      hidden by the FENCE pass  -> damage only if that fence never closes, which is
                                   structural state read back from the parser, not
                                   guessed from "is there content after it" (that
                                   guess false-alarmed on a closed fence at EOF).
      hidden by the SPAN pass   -> damage only if a reader-recognised question
                                   (`## ` heading or `- **[label]**` bullet) was
                                   swallowed too. A balanced quote swallows none.

    Rejected by measurement, in order: "any raw divider match" (fired on quoted
    banners); "everything after the divider is masked" (FALSE on the real damaged
    file — 4,542 unmasked tail chars); "masked to EOF" (fired on a closed fence at
    EOF). Each looked right and each was wrong on a real shape.
    """
    comments_only = mask_html_comments(text)
    fenced, fence_span = mask_fenced_code(comments_only, report_unclosed=True)
    # What the SPAN pass alone swallowed. `mask_markup` runs comments -> spans ->
    # fences, so diffing these two isolates the span step: fences are applied to
    # NEITHER side, and comments are applied to BOTH.
    spanned, span_ranges = _mask_nonfence_spans(comments_only, report_ranges=True)
    masked = mask_markup(text)
    hidden = []
    for m in divider.finditer(comments_only):
        if masked[m.start():m.end()].strip() != "":
            continue                                  # not hidden at all
        if fenced[m.start():m.end()].strip() == "":
            # The fence pass hid it — but only the UNCLOSED fence that actually
            # covers this divider implicates it. Asking "is any fence in the
            # document unclosed" warned on a divider safely inside a CLOSED
            # fenced example whenever an unrelated runaway fence appeared later
            # in the file. Same range-local shape the span branch below already
            # uses; this branch was the half that never got it.
            damaged = bool(fence_span
                           and fence_span[0] <= m.start()
                           and m.end() <= fence_span[1])
        else:
            # Scope the damage test to the pass that actually hid the divider.
            # Comparing raw-vs-all-maskers instead made any legitimately FENCED
            # `## ` heading elsewhere in the file look like span damage, so a
            # separate, balanced span quoting the divider warned on healthy
            # markup — reproduced by three reviewers at 06f3dfc4. The fence pass
            # has its own branch above; it must not leak into this one.
            # Only the span that actually hid THIS divider can implicate it.
            # Scoping to the pass was not enough: the test still ran across the
            # whole document, so an unrelated deliberate span elsewhere supplied
            # the "swallowed question" for a divider it never touched.
            damaged = any(
                _question_entry_masked_in(comments_only, spanned, lo, hi)
                for lo, hi in span_ranges
                if lo <= m.start() and m.end() <= hi
            )
        if damaged:
            hidden.append((text.count("\n", 0, m.start()) + 1, text[m.start():m.end()]))
    return hidden


def active_region(text: str, divider: re.Pattern = DIVIDER_RE) -> str:
    """`text` up to the first real (non-quoted) archive divider.

    Returns `text` unchanged when there is no divider — a file that keeps no audit
    trail is entirely active.

    THIRD failure mode (2026-08-03), distinct from the two in the module header: the
    divider is present and correctly spelled, but a single unbalanced backtick above
    it opens an inline span that closes on the next backtick far below, so
    `mask_markup` blanks the divider and it becomes unfindable. Measured on one host:
    two ticks 1,971 lines apart, the whole 2,951-line file served as active, retired
    entries re-surfacing as pending. Nothing errors and the file renders normally on
    GitHub — the only visible symptom is a waiting-count that moves when an unrelated
    section is edited.

    This WARNS and deliberately does NOT change the return value. Falling back to the
    raw match would cut at a `# Resolved` inside a fenced example — the exact thing
    masking exists to prevent — and that fails in the dangerous direction: live
    questions hidden, silently. The damaged state over-counts, which is noisy but
    safe. Noisy beats silent.
    """
    masked = mask_markup(text)
    m = divider.search(masked)
    if m:
        return text[:m.start()]
    hidden = _dividers_hidden_by_damage(text, divider)
    if hidden:
        # Report EVERY span-hidden candidate, with each one's own line and its own
        # matched text as the label. Reporting only the first was wrong twice
        # (qingyun-wu, review of #2558): the first raw match can be a QUOTED banner
        # that is masked on purpose, and the label was hard-coded to '# Resolved'
        # even under DIVIDER_OR_DONE_RE, so a real `# Done` divider was announced
        # under a name that is not in the file.
        #
        # A quoted banner and a real divider can BOTH survive comment/fence masking
        # and both be swallowed by the same runaway span, so nothing here can single
        # out "the real one" — and a diagnostic that guesses sends the reader to
        # harmless markup. Listing every candidate is the honest shape: the reader
        # is looking for one unbalanced backtick, and every line below is downstream
        # of it.
        where = ", ".join(f"{label!r} at line {line}" for line, label in hidden)
        print(
            f"pending-questions: divider {where} MASKED by markup above it (an "
            "unclosed backtick span or code fence), so the archive below is being "
            "served as LIVE. Fix the unbalanced markup at or above the FIRST line "
            "listed; see sonichi/sutando#2557.",
            file=sys.stderr,
        )
    return text
