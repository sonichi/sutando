"""The one policy for what an exception, a response or a status field may put in a log or a status file.

Sinks take structured safe fields only: an exception's type (plus the errno name of an OSError and
an HTTP status), the redacted gateway identity, and text the code itself wrote. Never an exception's
own text, a response body, or gateway-supplied free text. Behaviour code may keep those for control
flow; it never hands them to a sink. Stdlib only.
"""
from __future__ import annotations

import errno as _errno
import re
import urllib.parse
from typing import Any, Callable

WITHHELD = "<withheld>"
_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def code_field(value: Any, gateway_url: str = "") -> "str | None":
    """A gateway-supplied value usable as a structured field (an error name, an approval code):
    a short token of letters, digits and `_.-` that occurs in no form of the gateway URL. None
    otherwise, so free text, and anything that may be a piece of the URL's credentials, never
    reaches a sink."""
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        return None
    url = str(gateway_url or "")
    forms = {url, urllib.parse.unquote(url), urllib.parse.unquote(urllib.parse.unquote(url))}
    if any(value.lower() in form.lower() for form in forms if form):
        return None
    return value


def describe(exc: BaseException) -> str:
    """`<Type>[ <ERRNO>][ (HTTP <n>)]`, or the text a gateway call's scrubbed exception already
    carries (`<Type> contacting <redacted gateway>[ (HTTP <n>)]`). Never the exception's own text."""
    scrubbed = getattr(exc, "__dict__", {}).get("_scrubbed")
    if isinstance(scrubbed, str):
        return scrubbed
    out = type(exc).__name__
    code = getattr(exc, "errno", None) if isinstance(exc, OSError) else None
    if isinstance(code, int) and code in _errno.errorcode:
        out += f" {_errno.errorcode[code]}"
    status = getattr(exc, "__dict__", {}).get("code")
    if isinstance(status, int) and not isinstance(status, bool):
        out += f" (HTTP {status})"
    return out


def safe_status(value: Any, net: Callable[[str], str]) -> Any:
    """`value` with every string, at any depth, passed through `net` (the URL redactor); keys
    too. Numbers, booleans and None pass. Anything else is withheld, never str()-ed."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return net(value)
    if isinstance(value, dict):
        return {safe_status(k, net) if isinstance(k, str) else WITHHELD: safe_status(v, net)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_status(v, net) for v in value]
    return WITHHELD
