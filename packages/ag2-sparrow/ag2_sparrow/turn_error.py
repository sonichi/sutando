"""Why a local agent's turn failed, as one line its owner may read.

Codex and Claude Code adapters both report a failed turn's cause only to the owner, in the owner's DM
(owner_reason: the owner asked, and the task's room is a DM); anywhere else the plain line stands and
the cause is not sent. This is the one place that decides whether, and what of an error's text may be
shown: control characters dropped, secrets and local paths removed, delivery markers made inert, one
line, bounded. Identifiers an error names to be actionable (an email address, a request, organization,
room or user id) and slash commands such as `/login` are kept on purpose.
"""
from __future__ import annotations

import re
import sys
import unicodedata

from .chat_secret_filter import filter_chat_secrets
from .result_markers import neutralize_markers

LIMIT = 300
# Every pass below runs on at most this much of the message, so no pattern's cost grows with its length.
INPUT_CAP = 2000
# Terminal escape sequences (CSI, OSC, and two-character escapes), dropped whole before any other pass.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_SECRET_NAME = (r"""(?<![\w-])["']?[\w-]*?(?:key|token|secret|passw(?:or)?d|pass|pwd|auth(?:orization)?"""
                r"""|credentials?|sig(?:nature)?|cookie|session(?:[_-]?id)?)["']?\s*[:=]\s*""")
# A credential-looking name with its value: quoted (spaces included), or bare in a header, JSON, a query,
# a fragment or plain text. A comma inside a bare value is kept with it.
_KEY_QUOTED = re.compile(r"(%s)([\"'])(.*?)\2" % _SECRET_NAME, re.IGNORECASE)
_KEY_BARE = re.compile(r"(%s)((?:(?:Bearer|Basic)[\s:]+)?(?:[^\s\"'&,;}\]]|,(?=\S))+)" % _SECRET_NAME,
                       re.IGNORECASE)
# A value that is one plain lowercase word of at most 20 characters (`expired`, `invalid_grant`) says why,
# not what: it is kept, except after a name whose value is a password, secret, cookie or session.
_PLAIN_WORD = re.compile(r"[a-z]+(?:[_-][a-z]+)*")
_PLAIN_WORD_MAX = 20
_ALWAYS_SECRET = re.compile(r"pass|pwd|secret|private|cookie|session|credential", re.IGNORECASE)
_USERINFO = re.compile(r"(?<=://)[^\s/@]*(?::[^\s@]*)?@")
_BEARER = re.compile(r"\b(Bearer|Basic)\s+(?=[A-Za-z0-9._~+/=-]*[\d.])[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_PATH_START = (r"file:/+|(?<![\w./\\$%])(?:~|\$\{?HOME\}?(?=[/\\])|%\w+%(?=[/\\])|[A-Za-z]:[\\/]|\\\\"
               r"|/(?!/)(?=[\w.-]*/|[\w-]*\.\w))")
_SEG = r"[^\s'\"()<>\[\],;:]"
# A path names the owner's machine layout; a space-separated word that continues it with a separator
# (`Application Support/...`) or ends it as a file name (`secret plan.txt`) is still that path.
_PATH = re.compile(r"(?:%s)%s*(?: [^\s'\"()<>\[\],;:/\\]+[/\\]%s*| [^\s'\"()<>\[\],;:/\\.]+\.[A-Za-z0-9]{1,8}"
                   r"(?=[\s'\")\],;:]|$))*" % (_PATH_START, _SEG, _SEG))


def owner_reason(access_tier, channel_kind, message, limit: int = LIMIT) -> str:
    """The cause line for a task its owner asked in a DM; "" anywhere else, and when either is unknown.
    A failure's cause names the owner's account state (credit, limits, sign-in), so only the owner reads it."""
    in_dm = (channel_kind or "").strip().lower() == "dm"
    return error_line(message, limit) if access_tier == "owner" and in_dm else ""


def _value(match, quoted=False) -> str:
    value = match.group(3) if quoted else match.group(2)
    plain = len(value) <= _PLAIN_WORD_MAX and _PLAIN_WORD.fullmatch(value)
    if plain and not _ALWAYS_SECRET.search(match.group(1)):
        return match.group(0)
    return match.group(1) + (match.group(2) + "[REDACTED]" + match.group(2) if quoted else "[REDACTED]")


def error_line(message, limit: int = LIMIT) -> str:
    """`message` made safe for its reader: one line, at most `limit` characters; "" when nothing usable is left."""
    if not isinstance(message, str):
        return ""
    text = message
    if len(text) > INPUT_CAP:
        # Cut back to whitespace too, so a secret the cut splits is dropped whole, never left as a prefix;
        # an over-long run with no whitespace is dropped entirely.
        head = text[:INPUT_CAP].split()
        text = " ".join(head[:-1]) if len(head) > 1 else ""
    text = _ANSI.sub("", text)
    text = "".join(" " if ch.isspace() else "" if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    text = _KEY_QUOTED.sub(lambda m: _value(m, quoted=True), text)
    text = _KEY_BARE.sub(_value, text)
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
