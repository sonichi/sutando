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
  * `mask_code_spans`     — `` `...` ``, may wrap    (live on a third host, same day)

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

# Inline code span: N backticks ... N backticks, non-greedy, may span a line break
# (a wrapped sentence is exactly how this bit us). Longest-run-first so ``a`b``
# is one span rather than two.
_CODE_SPAN_RE = re.compile(r'(`+)(?:(?!\1).)*?\1', re.DOTALL)


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
            if (stripped and set(stripped) == {char} and len(stripped) >= size
                    and len(line) - len(line.lstrip(' ')) <= 3):
                in_fence = False
    return '\n'.join(out)


def mask_code_spans(text: str) -> str:
    """Blank inline code spans, preserving length and line breaks.

    A span may wrap across a line break, which is how `` # Resolved` `` reached
    column 0 on a real host while being, semantically, quoted text.
    """
    return _CODE_SPAN_RE.sub(lambda m: _blank(m.group(0)), text)


def mask_markup(text: str) -> str:
    """All three maskers, composed. Each preserves offsets, so order is safe."""
    return mask_code_spans(mask_fenced_code(mask_html_comments(text)))


def active_region(text: str, divider: re.Pattern = DIVIDER_RE) -> str:
    """`text` up to the first real (non-quoted) archive divider.

    Returns `text` unchanged when there is no divider — a file that keeps no audit
    trail is entirely active.
    """
    m = divider.search(mask_markup(text))
    return text[:m.start()] if m else text
