"""The one policy for what an exception, a response or a status field may put in a log or a status file.

Sinks take structured safe fields only: an exception's type (with its reason's type, an errno name,
an HTTP status and where it was raised), the redacted gateway identity, and text the code wrote. Never an exception's
own text, a response body, or gateway-supplied free text. Behaviour code may keep those for control
flow; it never hands them to a sink. Stdlib only.
"""
from __future__ import annotations

import errno as _errno
import os
import re
import socket
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
    gateway = fields.get("_gateway")
    if isinstance(gateway, str):
        out += f" contacting {gateway}"
    status = fields.get("code")
    if isinstance(status, int) and not isinstance(status, bool):
        out += f" (HTTP {status})"
    if tb is not None:
        out += f" at {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
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
