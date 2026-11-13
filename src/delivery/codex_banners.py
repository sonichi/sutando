"""The Codex CLI's own error cells that leave a seat unable to work, read off a captured pane.

Codex prints a refused turn as an error cell ("■ <message>") above an idle composer, so the
pane reads idle-ready to pane_gate while the seat is in fact limited, signed out, or refused.
Only the CLI's real wording is matched (codex-cli 0.160.1, its binary's strings), and only the
cell that is the pane's last content above the composer: an older cell under later turns is
history, not the seat's state now.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

USAGE_LIMIT, SIGNED_OUT, CONTENT_REFUSAL = "usage-limit", "signed-out", "content-refusal"
CAUSES = (USAGE_LIMIT, SIGNED_OUT, CONTENT_REFUSAL)

_APOS = "['’]"
# Each variant the binary carries: "You've hit your usage limit." then Upgrade to Plus/Pro,
# purchase more credits, ask your admin, or "for <model>. Switch to another model now".
_PATTERNS = (
    (USAGE_LIMIT, re.compile(rf"^You{_APOS}ve hit your usage limit\b", re.I)),
    # "Your access token could not be refreshed[ because …]. Please [log out and ]sign in again."
    (SIGNED_OUT, re.compile(r"^Your access token could not be refreshed\b.*\bsign in again\b", re.I)),
    (CONTENT_REFUSAL, re.compile(rf"^This content can{_APOS}t be shown\b", re.I)),
)
_CELL = re.compile(r"^■\s+(?P<text>\S.*)$")
_SGR = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# The composer row (› or ») and the model footer row below it.
_COMPOSER = re.compile(r"^\s*[›»](?:\s|$)")
_FOOTER = re.compile(r"^[ \t]*(?:gpt-[\w.-]+|codex[\w.-]*|o[1-9][\w.-]*)\b[^\n]*[·•]", re.I)
# "… or try again at 3:05 PM." / "Try again at Nov 13th, 2026 9:12 AM."
_RETRY_AT = re.compile(r"\btry again at\s+(?P<when>[^\n]+?)\.?\s*$", re.I)


@dataclass(frozen=True)
class Banner:
    cause: str
    line: str                 # the cell's own words, as the owner would read them
    reset_words: Optional[str] = None   # "3:05 PM" from "try again at 3:05 PM", else None


_ROW_GLYPH = re.compile(r"^\s*[◦•✔✗└├│⠀-⣿✻✶✳✢·]")  # a spinner, bullet or tool row, never a cell's own line
MAX_BLANKS = 2  # blank lines between the cell and the composer


def live_banner(capture: Optional[str]) -> Optional[Banner]:
    """The Codex error cell the seat is stopped on, or None. It must be the last block before the
    composer: up to MAX_BLANKS blank lines and one hint row may sit between them, never a spinner,
    bullet or tool row (a turn still running). The cell is its "■ " row at column 0 and the lines
    of its own below it, indented (a wrap may indent deeper). Above it is a blank line or the top
    of the capture; what is above that (the prompt, tool rows, an assistant line, an older cell)
    is any turn's own output. Codex draws history indented or under a glyph, so a "■" at column 0
    that a log or a message printed sits right under its own row with no blank line between."""
    if not capture:
        return None
    lines = [_SGR.sub("", ln).rstrip() for ln in capture.splitlines()]
    composer = next((i for i in range(len(lines) - 1, -1, -1) if _COMPOSER.match(lines[i])), None)
    if composer is None:
        return None
    i, blanks, hint = composer - 1, 0, False
    while i >= 0 and (not lines[i].strip() or (lines[i].startswith(" ") and not hint)):
        if _ROW_GLYPH.match(lines[i]):
            return None  # a turn is running under the cell
        if not lines[i].strip():
            blanks += 1
        elif i - 1 >= 0 and not lines[i - 1].strip():
            hint = True  # one hint row, set off by a blank line; one right under the cell is its own
        else:
            break
        i -= 1
    if blanks > MAX_BLANKS:
        return None
    own = []
    while i >= 0 and lines[i].startswith("  ") and lines[i].strip() and not _ROW_GLYPH.match(lines[i]):
        own.insert(0, lines[i].strip())
        i -= 1
    cell = _CELL.match(lines[i]) if i >= 0 else None
    if cell is None:
        return None
    if i > 0 and lines[i - 1].strip():
        return None  # printed under its own row: a log or a message, not a cell
    text = " ".join([cell["text"].strip(), *own]).strip()
    for cause, pattern in _PATTERNS:
        if pattern.search(text):
            found = _RETRY_AT.search(text) if cause == USAGE_LIMIT else None
            return Banner(cause, text, found["when"].strip() if found else None)
    return None
