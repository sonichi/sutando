"""Links a person clicks to open an AG2 Space room, message or room surface inside the app.

The app opens a matrix.to room or message link as a room mention, and its own room route
(`ROOM_ROUTE`) with `?surface=` in place; `<origin>/#/room/<room>` is no route of it, so it
opened outside. Standard library only: skills import it without the rest of core.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
from typing import Optional

try:
    import dotspace
except ImportError:  # imported by file path: the sibling resolver sits in this directory
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dotspace

MATRIX_TO = "https://matrix.to/#/"
# The app's route section for a room; a URL path segment, not a filesystem path.
ROOM_ROUTE = "home"
_ORIGIN = re.compile(r"^(https?://[^/?#\s]+)")
_ROOM = re.compile(r"^[!#][^\s:]+:\S+$")
# The database ids a link may name in the app; it opens one only if the room has it.
_DB_PAGE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def default_via(room_id: str = "") -> str:
    """`?via=` for `room_id`: its own server's DotSpace (rooms are `!x:server`), else the install's."""
    if dotspace.server_of_matrix_id(room_id):
        return dotspace.link_via_for(room_id, fetch=dotspace.NO_FETCH)
    return dotspace.default_link_via(primary_server=_primary_server())


def _primary_server() -> str:
    """The primary connection's server, "" where core's connections record cannot be read."""
    try:
        from connections import primary_server
        return primary_server()
    except Exception:  # noqa: BLE001 - a skill importing this without core falls back to the legacy via
        return ""


def __getattr__(name: str):
    # `DEFAULT_VIA` stays importable; it is the install's via, resolved on each read.
    if name == "DEFAULT_VIA":
        return default_via()
    raise AttributeError(name)


def room_link(room_id: str, event_id: str = "", via: Optional[str] = None) -> str:
    """`https://matrix.to/#/<room>[/<event>][?via=<server>]`; `via` None takes the room's
    server's DotSpace (`default_via`), empty `via` drops the query."""
    via = default_via(room_id) if via is None else via.strip().lower()
    if via and not dotspace.is_server_name(via):
        via = ""  # an explicit via that is not a server name never reaches the query
    frag = f"{room_id}/{event_id}" if event_id else room_id
    return f"{MATRIX_TO}{frag}?via={via}" if via else f"{MATRIX_TO}{frag}"


def surface_link(origin: str, room_id: str, surface: str, page: str | None = None) -> str:
    """`<origin>/<ROOM_ROUTE>/<room, encodeURIComponent>?surface=<surface>[&page=<page>]`.

    Raises ValueError for an origin that is not an http(s) URL or a room that is no room id/alias."""
    m = _ORIGIN.match((origin or "").strip())
    if not m:
        raise ValueError(f"not an app origin: {origin!r}")
    if not _ROOM.match(room_id or ""):
        raise ValueError(f"not a room id or alias: {room_id!r}")
    query = {"surface": surface, **({"page": page} if page else {})}
    room_path = urllib.parse.quote(room_id, safe="-_.!~*'()")
    return "/".join((m.group(1), ROOM_ROUTE, room_path)) + "?" + urllib.parse.urlencode(query)


def database_link(origin: str, room_id: str, db_id: str | None = None) -> str:
    """The room's Database surface, on database `db_id` when that id is one a link may name."""
    page = (db_id or "").strip().lower()
    return surface_link(origin, room_id, "database", page if _DB_PAGE.fullmatch(page) else None)
