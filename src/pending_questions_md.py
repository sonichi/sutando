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

So the discriminator is not how the divider ENDS, it is whether the match is inside
a comment. Real dividers never are; the documentation of the divider always is.

`mask_html_comments` blanks comment bodies while preserving BOTH length and line
count, so a caller can search the masked text and slice the ORIGINAL by the same
offset. That matters: agent-api derives question identity from section bodies, and
stripping (rather than masking) would shift offsets and silently change ids.

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


def mask_html_comments(text: str) -> str:
    """Blank the contents of HTML comments, preserving length and line breaks.

    Offsets into the result are valid offsets into `text`, so callers may search
    here and slice there.
    """
    return _COMMENT_RE.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text)


def active_region(text: str, divider: re.Pattern = DIVIDER_RE) -> str:
    """`text` up to the first real (non-commented) archive divider.

    Returns `text` unchanged when there is no divider — a file that keeps no audit
    trail is entirely active.
    """
    m = divider.search(mask_html_comments(text))
    return text[:m.start()] if m else text
