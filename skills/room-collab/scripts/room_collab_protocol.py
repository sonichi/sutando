"""The wire format, with no dependencies and no I/O.

Separate from the client because these are the parts that fail *silently*: a
varint off by one, or a room id quoted wrong, raises nothing — it produces a
frame the server discards and a document that simply never syncs. Keeping them
importable without pycrdt or websockets is what lets CI exercise them.
"""
from __future__ import annotations

import urllib.parse

DEFAULT_TEXT_NAME = "markdown"
DEFAULT_KIND = "markdown"
HTML_KIND = "html"
# The surfaces that are one shared text, and the root each text lives under.
TEXT_ROOTS = {DEFAULT_KIND: DEFAULT_TEXT_NAME, HTML_KIND: "html"}

# The service accepts the socket and THEN closes with one of these, because a
# close before accept cannot carry a code the client can read.
CLOSE_BAD_ROOM, CLOSE_FORBIDDEN, CLOSE_BAD_KIND = 4400, 4403, 4404

CLOSE_REASONS = {
    CLOSE_BAD_ROOM: "the room id is malformed (4400) — this is a refusal, not an empty surface",
    CLOSE_FORBIDDEN: ("access refused or withdrawn (4403): this credential is not authorized "
                      "for the room's surfaces, or membership/write power changed. It can also mean "
                      "core-api was briefly unreachable, so one 4403 is not proof of revocation"),
    CLOSE_BAD_KIND: "the surface kind is malformed (4404)",
}


class RoomDocError(RuntimeError):
    """A refusal or protocol failure the caller can report verbatim.
    `code` is the websocket close code when one ended the session, else None;
    `status` is the HTTP status when the handshake itself was refused, else None."""

    code: int | None = None
    status: int | None = None


# "The service went away, not you" — a restart, a proxy leaving, an abnormal
# drop. A watcher comes back from these; a 4xxx refusal is about the agent.
RECONNECT_CODES = frozenset({1001, 1006, 1011, 1012, 1013, 1014})
# The edge answered for a service that was not there: a rollout in progress.
RECONNECT_STATUSES = frozenset({502, 503, 504})


def close_code(exc: BaseException | None) -> int | None:
    if exc is None:
        return None
    return getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)


def http_status(exc: BaseException | None) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def unanswered(exc: BaseException | None) -> bool:
    """Nothing answered at all — the gap in a rollout between the old process
    leaving and the new one listening, a handshake that timed out, a name that
    does not resolve yet. A REFUSAL always answers, with a status or a code."""
    return isinstance(exc, OSError) and http_status(exc) is None


def is_transient(exc: BaseException | None) -> bool:
    """A failure a watcher rides out: a restart close, a handshake the edge
    refused because the service was mid-rollout, or no answer at all. A
    refusal is never transient."""
    return (getattr(exc, "code", None) in RECONNECT_CODES
            or getattr(exc, "status", None) in RECONNECT_STATUSES
            or getattr(exc, "transient", False) is True)


def write_var_uint(n: int) -> bytes:
    if n < 0:
        raise ValueError("var uint is unsigned")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def read_var_uint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(data):
            raise RoomDocError("truncated varint")
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def write_var_bytes(b: bytes) -> bytes:
    return write_var_uint(len(b)) + b


def read_var_bytes(data: bytes, i: int) -> tuple[bytes, int]:
    n, i = read_var_uint(data, i)
    if i + n > len(data):
        raise RoomDocError("truncated payload")
    return data[i:i + n], i + n


def doc_socket_url(api_root: str, room_id: str, kind: str = DEFAULT_KIND) -> str:
    """`https://host` (or ws(s)://) + a room id -> the surface's socket URL.

    The room id is one path segment and carries `!` and `:` by grammar, so it
    is quoted whole; a bare one would split the path.
    """
    origin = (api_root or "").rstrip("/")
    if not origin:
        raise RoomDocError("no API root given (pass --url or set AG2_ROOM_COLLAB_URL)")
    if origin.startswith("https://"):
        origin = "wss://" + origin[len("https://"):]
    elif origin.startswith("http://"):
        origin = "ws://" + origin[len("http://"):]
    if not origin.startswith(("ws://", "wss://")):
        raise RoomDocError(f"not an http(s) or ws(s) origin: {api_root!r}")
    # The collab path is the default; an origin that names the old path keeps it
    # for the alias window, since the service answers on both.
    if "/api/v1/room-collab" not in origin and "/api/v1/room-doc" not in origin:
        origin = f"{origin}/api/v1/room-collab"
    url = f"{origin}/{urllib.parse.quote(room_id, safe='')}/ws"
    # A room holds more than one surface; the kind selects which. The default
    # is sent bare, which is what every existing caller already produces.
    if kind and kind != DEFAULT_KIND:
        url += f"?kind={urllib.parse.quote(kind, safe='')}"
    return url


def _body_text(exc: Exception) -> str:
    body = getattr(getattr(exc, "response", None), "body", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    return (body or "").strip()


def explain(exc: Exception, url: str) -> str:
    """Turn the transport's error into the reason a caller can act on.

    Each status is a different problem with a different owner, and three of
    them look alike from outside: a service that has no record of the agent
    (401), a room that refuses it (403), and an edge proxy refusing before the
    service ever saw the request (also 403). Each line ends with who fixes it.
    """
    status = http_status(exc)
    body = _body_text(exc)
    if status in RECONNECT_STATUSES:
        return (f"not being served right now ({status}) at {url}\n"
                "The edge answered for a service that is restarting, being rolled out, "
                "or (504) too slow to answer. Nobody fixes this: a watcher retries on "
                "its own, a one-shot command is run again in a minute.")
    if status == 401:
        return (f"unknown to the service (401) at {url}: {body or 'the bearer was rejected'}\n"
                "The token was presented; this deployment has no record of the agent behind "
                "it. That is provisioning, not room permission — ask whoever runs this "
                "deployment to register the agent. A different deployment may accept the "
                "same token.")
    if status == 403 and "error code: 1010" in body:
        return (f"refused by the edge (403, Cloudflare 1010) in front of {url}\n"
                "core-api never saw this request, so it says nothing about room access. "
                "The socket path is the supported one; a plain HTTP probe of a websocket "
                "route trips this.")
    if status == 403:
        return (f"refused (403) by {url}: {body or 'no detail given'}\n"
                "The service knows this agent and the room refuses it: below write power, "
                "or not authorized for the room's surfaces. A room admin fixes this, not a token.")
    if status == 404:
        return (f"not found (404) at {url}\n"
                "Either the room id is wrong or this account is not a member — "
                "non-members are told 404 so a room's existence stays hidden. Check the "
                "id, then ask for an invite.")
    if status == 426:
        return f"{url} answers websocket only (426): use the client, not an HTTP probe."
    return f"cannot open {url}: {type(exc).__name__}: {exc}"


def close_reason(exc: BaseException | None) -> str:
    """Name what ended a session, in the client's own terms.

    The transport reports a number; a caller needs to know whether to fix the
    room id, the credential, or nothing at all.
    """
    if exc is None:
        return "the server closed the connection"
    code = close_code(exc)
    if code in CLOSE_REASONS:
        return CLOSE_REASONS[code]
    if code:
        return f"the server closed the connection with code {code}"
    return f"{type(exc).__name__}: {exc}"
