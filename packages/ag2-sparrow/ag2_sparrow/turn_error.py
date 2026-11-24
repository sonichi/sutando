"""Why a local agent's turn failed, as one line a room may read.

Codex and Claude Code adapters both post a failed turn's cause in the room; this is the one place that
decides what of an error's text may go there: secrets and local paths removed, one line, bounded.
"""
from __future__ import annotations

import re
import sys

from .chat_secret_filter import filter_chat_secrets

LIMIT = 300
# An absolute or home-relative filesystem path (POSIX or Windows) names the owner's machine layout.
_PATH = re.compile(r"(?:(?<![\w.:/])~|(?<![\w.:/])[A-Za-z]:\\|(?<![\w.:/])/(?=[\w.-]+/))[^\s'\"()<>,;:]*")
_URL_SECRET = re.compile(r"([?&](?:[\w-]*(?:key|token|secret|sig|signature|password|auth)[\w-]*)=)[^&\s]+",
                         re.IGNORECASE)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def error_line(message, limit: int = LIMIT) -> str:
    """`message` made safe for a room: one line, at most `limit` characters; "" when nothing usable is left."""
    if not isinstance(message, str):
        return ""
    text = filter_chat_secrets(message).text
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    text = _URL_SECRET.sub(r"\1[REDACTED]", text)
    text = _PATH.sub("<path>", text)
    text = " ".join(text.split())
    text = _scrub_registered(text)
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def _scrub_registered(text: str) -> str:
    # Secrets are registered only in a process that loaded diagnostics; importing it here would put every
    # adapter that reports a turn error in gateway-credential scope.
    diagnostics = sys.modules.get(f"{__package__}.diagnostics")
    return diagnostics.scrub(text) if diagnostics is not None else text
