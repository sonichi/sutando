"""Where the proactive drain posts a body, as a dependency-light policy.

The drain and any adapter that queues a proactive file ask here, so an adapter
never accepts a destination the drain would skip."""
from __future__ import annotations

import re

from pathlib import Path

from .outbox import valid_pid
from .result_markers import parse_markers

# Destination FORMAT validation is the bridge's own job ("the bridge
# validates the id format for its platform when applying" — result_markers).
MATRIX_ROOM_RE = re.compile(r"^![^\s:]+:\S+$")
# Where the drain records a confirmed proactive delivery, keyed by the file's stem.
RECEIPT_DIR = ".outbox-ag2space-proactive"
# Where the drain parks a line it will never send, as `<stem>-<epoch>.txt`.
UNDELIVERED_DIR = "undelivered"
# The drain claims a queued `<stem>.txt` by renaming it to `<stem>.sending.<pid>`.
CLAIM_INFIX = ".sending."


def claim_path(queued: Path, pid: int) -> Path:
    """The name the drain renames `queued` to while process `pid` sends it; ValueError for a pid
    outside `valid_pid`, whose name `claim_pid` would reject."""
    if valid_pid(pid) is None:
        raise ValueError(f"pid {pid!r} is outside the pid domain")
    return queued.with_suffix(f"{CLAIM_INFIX}{pid}")


def claim_pid(name: str) -> "int | None":
    """The pid of a drain claim name; None for any other name, so no reader treats a
    file the drain neither creates nor recovers as a claim."""
    parts = name.rsplit(CLAIM_INFIX, 1)
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        return None
    pid = valid_pid(int(parts[1]))
    # Only the spelling claim_path writes: no leading zeros.
    return pid if pid is not None and str(pid) == parts[1] else None


def claim_base(name: str) -> "str | None":
    """The queued stem a drain claim name came from (the inverse of claim_path); None for any name
    claim_path never writes. `<stem>.sending.x.sending.<pid>` is a claim of `<stem>.sending.x`."""
    if claim_pid(name) is None:
        return None
    return name.rsplit(CLAIM_INFIX, 1)[0]


def route(body: str, skip_after_channel: bool = True) -> "tuple[str, str | None, str]":
    """('send', room_or_None, stripped-body) | ('foreign', None, '') |
    ('drop', None, '').

    Marker grammar comes SOLELY from parse_markers() (no private parser —
    CLAUDE.md result-marker contract); this function only applies the actions
    this transport supports:
      * skip markers   → 'drop' (archive silently, deliver nothing)
      * [dm-only]      → parse_markers already suppressed any redirect, so the
                         body falls through to the default (owner) room
      * [channel: !r:s]→ 'send' to that room, marker stripped
      * [channel: C…/digits] → 'foreign' — that bridge owns the file (review
                         blocker: claiming it here would leak the raw body)
      * attach markers → stripped by the parser; uploads are unsupported on
                         the room-message op, so the actions are ignored
      * [thread: $root]→ stripped here; the send reads it via
                         _proactive_thread_root and posts in that thread
      * bare [thread]  → task results only; stripped and ignored here
    """
    parsed = parse_markers(body, skip_after_channel=skip_after_channel)
    if any(a.kind == "skip" for a in parsed.actions):
        return ("drop", None, "")
    redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
    if redirect is not None:
        dest = redirect.value
        if MATRIX_ROOM_RE.match(dest):
            return ("send", dest, parsed.body)
        return ("foreign", None, "")
    return ("send", None, parsed.body)


def deliverable_here(room: str, own_homeserver: str) -> bool:
    """A gateway posts only to rooms on its own homeserver. Another lane's room
    must be left for that lane: claiming it here fails at the gateway and the
    retry budget then parks deliverable work as undeliverable."""
    if not own_homeserver:
        return True  # identity unknown: today's behaviour, deliberately
    if ":" not in room:
        return False  # names no server: deliverable nowhere, strands visibly
    return room.split(":", 1)[1] == own_homeserver


def homeserver_of(mxid: str) -> str:
    """The server part of `@user:server`; FIRST colon, since it may carry a port or IPv6."""
    return mxid.split(":", 1)[1] if mxid.startswith("@") and ":" in mxid else ""


def drain_room(body: str, own_homeserver: str) -> "str | None":
    """The room the drain would post `body` to, or None when it would not post it to a room
    it names: dropped, another bridge's, unaddressed, or on another homeserver."""
    kind, room, _ = route(body)
    if kind != "send" or room is None or not deliverable_here(room, own_homeserver):
        return None
    return room
