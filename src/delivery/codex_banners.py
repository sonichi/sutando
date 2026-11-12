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


def live_banner(capture: Optional[str]) -> Optional[Banner]:
    """The Codex error cell the seat is stopped on, or None: the cell must be the last content
    above the composer (its continuation lines, indented, belong to it), and match one of the
    CLI's own messages."""
    if not capture:
        return None
    lines = [_SGR.sub("", ln).rstrip() for ln in capture.splitlines()]
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):  # the composer: the last row it is drawn on
        if _COMPOSER.match(lines[i]):
            end = i
            break
    else:
        return None
    body = [ln for ln in lines[:end] if ln.strip()]
    # Walk up from the composer over the cell's indented continuation lines to its "■" row.
    for j in range(len(body) - 1, -1, -1):
        cell = _CELL.match(body[j])
        if cell:
            tail = [ln.strip() for ln in body[j + 1:]]  # only its own indented lines (below)
            text = " ".join([cell["text"].strip(), *tail]).strip()
            for cause, pattern in _PATTERNS:
                if pattern.search(text):
                    found = _RETRY_AT.search(text) if cause == USAGE_LIMIT else None
                    return Banner(cause, text, found["when"].strip() if found else None)
            return None
        if not body[j].startswith(" ") or _FOOTER.match(body[j]):
            return None  # the last content above the composer is not an error cell
    return None
