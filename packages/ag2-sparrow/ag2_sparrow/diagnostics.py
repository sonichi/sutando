"""The one policy for what an exception, a response or a status field may put in a log or a status file.

Sinks take structured safe fields only: an exception's type (with its reason's type, an errno name,
an HTTP status and where it was raised), the redacted gateway identity, and text the code wrote. Never an exception's
own text, a response body, or gateway-supplied free text. Behaviour code may keep those for control
flow; it never hands them to a sink. Stdlib only.
"""
from __future__ import annotations

import errno as _errno
import math
import os
import re
import socket
import threading
import urllib.parse
from typing import Any, Callable

WITHHELD = "<withheld>"
WITHHELD_TEXT = "<text withheld: it may contain a credential>"
_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
BARE_SECRET_MIN = 8  # a bare URL query value shorter than this would withhold ordinary words
_SECRETS: "dict[str, frozenset[str]]" = {}  # the process's secrets -> every rendering of each
_SECRETS_LOCK = threading.Lock()
SCRUBBED_TYPES: "set[type]" = set()  # exception classes gateway_instance.scrub_exception made


def fold(text: str) -> str:
    """The form secrets are matched in: case-folded. A token is case-sensitive as a credential,
    but an echo of it in another case still reveals it."""
    return str(text).casefold()


def forms(secret: str) -> "set[str]":
    """The ways a secret can be rendered in diagnostic text: raw, escaped as repr and
    unicode_escape render it, and percent-encoded or -decoded."""
    out = set()
    for s in {secret, urllib.parse.unquote(secret), urllib.parse.unquote_plus(secret)}:
        out |= {s, repr(s)[1:-1], s.encode("unicode_escape").decode("ascii"),
                ascii(s)[1:-1], s.encode("ascii", "backslashreplace").decode("ascii"),
                *(_quoted(quote, s, safe) for quote in (urllib.parse.quote, urllib.parse.quote_plus)
                  for safe in ("", "/=&?#:@"))}
    return {f for f in out if f}


def _quoted(quote: Callable[..., str], s: str, safe: str) -> str:
    # An env value read from non-UTF-8 bytes holds lone surrogates, which strict UTF-8 refuses.
    try:
        return quote(s, safe=safe, errors="surrogateescape")
    except UnicodeEncodeError:
        return quote(s, safe=safe, errors="surrogatepass")


def register_secret(value: Any, min_len: int = 1) -> None:
    """Add a secret of this process. A token (the bearer, the media token, a token read from a
    file) is opaque and matched at any length. A bare piece of the gateway URL passes
    min_len=BARE_SECRET_MIN, so a short query value does not withhold ordinary words; its
    delimited form (`=value`, `user:pw@`) is registered too, at any length."""
    if not isinstance(value, str) or not value or len(value) < min_len:
        return
    rendered = frozenset(fold(f) for f in forms(value))
    with _SECRETS_LOCK:
        _SECRETS[value] = rendered


def _views(text: str) -> "set[str]":
    return {fold(v) for v in (text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text))}


def contains_secret(text: str) -> bool:
    """Whether any registered secret survives in `text`, in any rendering."""
    with _SECRETS_LOCK:
        rendered = list(_SECRETS.values())
    views = _views(str(text))
    return any(f in v for fs in rendered for f in fs for v in views)


def holds_or_is_piece(value: str) -> bool:
    """Whether `value` holds a registered secret, or is itself a piece of one, in any rendering and
    letter case: the rule for a short gateway-supplied token that will be kept or shown."""
    token = fold(value)
    with _SECRETS_LOCK:
        rendered = [f for fs in _SECRETS.values() for f in fs]
    return contains_secret(value) or any(token in f for f in rendered)


def scrub(text: str) -> str:
    """`text`, or WITHHELD_TEXT when a registered secret survives in it."""
    return WITHHELD_TEXT if contains_secret(text) else str(text)


def code_field(value: Any, gateway_url: str = "") -> "str | None":
    """A gateway-supplied value usable as a structured field (an error name, an approval code):
    a short token of letters, digits and `_.-` that occurs in no form of the gateway URL, holds
    no registered secret and is no piece of one, in any letter case. None otherwise, so free
    text, a credential of the URL and an echoed bearer token never reach a sink."""
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        return None
    url = str(gateway_url or "")
    url_forms = {url, urllib.parse.unquote(url), urllib.parse.unquote(urllib.parse.unquote(url))}
    token = fold(value)
    if any(token in fold(form) for form in url_forms if form):
        return None
    return None if holds_or_is_piece(value) else value


_EAI = {v: k for k, v in vars(socket).items() if k.startswith("EAI_") and isinstance(v, int)}


def _errno_name(exc: BaseException) -> str:
    code = getattr(exc, "errno", None) if isinstance(exc, OSError) else None
    if not isinstance(code, int):
        return ""
    # A resolver error carries an EAI_* code, not an errno.
    return _EAI.get(code, "") if isinstance(exc, socket.gaierror) else _errno.errorcode.get(code, "")


def _innermost(exc: BaseException):
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb


def describe(exc: BaseException) -> str:
    """`<Type>[(<reason type>/<ERRNO>)][ <ERRNO>][[<key>]][ contacting <redacted gateway>][ (HTTP <n>)]
    [ at <file>:<line>]`. Never the exception's own text: a KeyError's key only when the failing
    code wrote it as a string literal, a location only as the innermost frame's file name and line."""
    fields = getattr(exc, "__dict__", {})
    out = type(exc).__name__
    reason = fields.get("reason")
    if isinstance(reason, BaseException):
        name = _errno_name(reason)
        out += f"({type(reason).__name__}{'/' + name if name else ''})"
    elif _errno_name(exc):
        out += f" {_errno_name(exc)}"
    tb = _innermost(exc)
    if isinstance(exc, KeyError) and len(exc.args) == 1 and isinstance(exc.args[0], str) and tb is not None \
            and exc.args[0] in tb.tb_frame.f_code.co_consts:
        out += f"[{exc.args[0]!r}]"
    gateway = fields.get("_gateway") if type(exc) in SCRUBBED_TYPES else None
    if isinstance(gateway, str):
        out += f" contacting {scrub(gateway)}"
    status = fields.get("code")
    if isinstance(status, int) and not isinstance(status, bool):
        out += f" (HTTP {status})"
    if tb is not None:
        out += f" at {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
    return out


# PEP 440's version pattern (packaging.version.VERSION_PATTERN), matched whole.
_PEP440_RE = re.compile(r"""
    v?(?:(?:[0-9]+)!)?[0-9]+(?:\.[0-9]+)*
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?
    (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?
    (?:[-_.]?dev[-_.]?[0-9]*)?
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?
""", re.VERBOSE | re.IGNORECASE)


def version_field(value: Any) -> "str | None":
    """A gateway-supplied version usable as a structured field: a PEP 440 version of at most
    64 characters that holds no registered secret. None otherwise."""
    if not isinstance(value, str) or len(value) > 64 or not _PEP440_RE.fullmatch(value):
        return None
    return None if contains_secret(value) else value


def safe_status(value: Any, net: Callable[[str], str]) -> Any:
    """`value` with every string, at any depth, passed through `net` (the URL redactor) and then
    the secret registry; keys through `net` only. Finite numbers, booleans and None pass. Anything else is
    withheld, never str()-ed."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else WITHHELD
    if isinstance(value, str):
        return scrub(net(value))
    if isinstance(value, dict):
        # Keys are field names: the URL net runs on them, the registry on values only, so a short
        # token cannot turn the payload's own structure into withheld keys.
        return {net(k) if isinstance(k, str) else WITHHELD: safe_status(v, net) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_status(v, net) for v in value]
    return WITHHELD
