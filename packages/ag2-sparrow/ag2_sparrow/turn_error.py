"""Why a local agent's turn failed, as one line a room may read.

Codex and Claude Code adapters both post a failed turn's cause in the room when the owner asked
(owner_reason; anyone else gets the plain line); this is the one place that decides whether, and what
of an error's text may go there: control characters dropped, secrets and local paths
removed, delivery markers made inert, one line, bounded. Identifiers an error names to be actionable
(an email address, a request, organization, room or user id) are kept on purpose.
"""
from __future__ import annotations

import re
import sys
import unicodedata

from .chat_secret_filter import filter_chat_secrets
from .result_markers import neutralize_markers

LIMIT = 300
# Terminal escape sequences (CSI, OSC, and two-character escapes), dropped whole before any other pass.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
# A credential-looking name with its value, in a header, JSON, a query, a fragment or bare text.
_KEY_VALUE = re.compile(
    r"""((?<![\w-])["']?[\w-]*?(?:key|token|secret|passw(?:or)?d|auth(?:orization)?|credentials?|sig(?:nature)?)"""
    r"""["']?\s*[:=]\s*["']?)(?:(?:Bearer|Basic)[\s:]+)?[^\s"'&,;}\]]+""", re.IGNORECASE)
_USERINFO = re.compile(r"(?<=://)[^/\s@:]+:[^/\s@]+@")
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_PATH_START = (r"file://|(?<![\w.:/\\$%])(?:~|\$\{?HOME\}?(?=[/\\])|%\w+%(?=[/\\])|[A-Za-z]:[\\/]|\\\\"
               r"|/(?=[\w.-]))")
# A path names the owner's machine layout; a space-separated word that continues it with a separator
# (`Application Support/...`) is still that path.
_PATH = re.compile(r"(?:%s)[^\s'\"()<>\[\],;:]*(?: [^\s'\"()<>\[\],;:/\\]+[/\\][^\s'\"()<>\[\],;:]*)*" % _PATH_START)


def owner_reason(access_tier, message, limit: int = LIMIT) -> str:
    """The cause line for a task its owner asked; "" for any other tier, and for an unknown one.
    A failure's cause names the owner's account state (credit, limits, sign-in), so only the owner reads it."""
    return error_line(message, limit) if access_tier == "owner" else ""


def error_line(message, limit: int = LIMIT) -> str:
    """`message` made safe for a room: one line, at most `limit` characters; "" when nothing usable is left."""
    if not isinstance(message, str):
        return ""
    text = _ANSI.sub("", message)
    text = "".join(" " if ch.isspace() else "" if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    text = _KEY_VALUE.sub(r"\1[REDACTED]", text)
    text = _USERINFO.sub("[REDACTED]@", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    text = filter_chat_secrets(text).text
    text = _PATH.sub("<path>", text)
    text = " ".join(text.split())
    text = _scrub_registered(text)
    text = neutralize_markers(text)
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def _scrub_registered(text: str) -> str:
    # Secrets are registered only in a process that loaded diagnostics; importing it here would put every
    # adapter that reports a turn error in gateway-credential scope.
    diagnostics = sys.modules.get(f"{__package__}.diagnostics")
    return diagnostics.scrub(text) if diagnostics is not None else text
