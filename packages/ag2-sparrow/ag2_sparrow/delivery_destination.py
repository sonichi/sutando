"""Where the proactive drain posts a body, as a dependency-light policy.

The drain and any adapter that queues a proactive file ask here, so an adapter
never accepts a destination the drain would skip."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time

from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

from .outbox import ITEMS_DIR, list_items, valid_pid
from .proactive_routing import PROACTIVE_OUTBOX_DIRNAME
from .result_markers import parse_markers

# Destination FORMAT validation is the bridge's own job ("the bridge
# validates the id format for its platform when applying" — result_markers).
MATRIX_ROOM_RE = re.compile(r"^![^\s:]+:\S+$")
# The proactive lane's outbox root: one item per file stem and body, `<stem>~<digest>`.
RECEIPT_DIR = PROACTIVE_OUTBOX_DIRNAME
# Where the drain parks a line it will never send, as `<stem>-<epoch>.txt`.
UNDELIVERED_DIR = "undelivered"
# The drain claims a queued `<stem>.txt` by renaming it to `<stem>.sending.<pid>.<ns>`.
CLAIM_INFIX = ".sending."
# A directory of its own, so no reader listing the results dir sees a lock file.
SEND_LOCK_DIR = ".proactive-send-lock"
# The protocol a drain that sends under send_lock, and sends a hold line only once armed, publishes
# in its lane record; a producer relying on it refuses to act for a live drain without it.
SEND_LOCK_PROTOCOL = 3
# The drain's whole send under the send lock, every phase included.
SEND_DEADLINE_S = 15.0
# The longest a producer waits for the send lock, above the drain's longest send under it; the
# drain waits briefly and hands the file back.
PRODUCER_LOCK_WAIT_S = 30.0
DRAIN_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.05



class SendLockUnavailable(OSError):
    """The send lock could not be taken; nothing under it ran."""


@contextlib.contextmanager
def send_lock(result_dir: Path, wait: "float | None" = None):
    """Exclusive lock on the fixed file `result_dir`/SEND_LOCK_DIR/lock, which is never unlinked, so
    every process and restart locks the same inode. The drain holds it across the outbox's
    dispatch-time recheck and send, and a producer holds it while it commits an answer or a
    message about a task; acquisition is the linearization point. Released on every exit; SendLockUnavailable when it cannot be taken
    within `wait` seconds. A platform without flock (no Hatch there) skips it."""
    if fcntl is None:
        yield
        return
    wait = PRODUCER_LOCK_WAIT_S if wait is None else wait
    try:
        lock_dir = Path(result_dir) / SEND_LOCK_DIR
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(lock_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise SendLockUnavailable(str(e)) from e
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SendLockUnavailable(f"held by another sender past {wait:g}s") from None
                time.sleep(_LOCK_POLL_S)
            except OSError as e:
                raise SendLockUnavailable(str(e)) from e
        yield
    finally:
        os.close(fd)


def claim_pid(name: str) -> "int | None":
    """The pid of a drain claim name, `<stem>.sending.<pid>` or `<stem>.sending.<pid>.<ns>`; None
    for any other name, so no reader treats a file the drain neither creates nor recovers as a claim."""
    parts = name.rsplit(CLAIM_INFIX, 1)
    if len(parts) != 2 or not parts[1].isascii():
        return None
    pid_text, dot, ns = parts[1].partition(".")
    if not pid_text.isdigit() or (dot and not ns.isdigit()):
        return None
    pid = valid_pid(int(pid_text))
    return pid if pid is not None and str(pid) == pid_text else None


def claim_base(name: str) -> "str | None":
    """The queued stem a drain claim name came from; None for any name the drain never writes.
    `<stem>.sending.x.sending.<pid>` is a claim of `<stem>.sending.x`."""
    if claim_pid(name) is None:
        return None
    return name.rsplit(CLAIM_INFIX, 1)[0]


def lane_items(result_dir: Path, stem: str) -> "list[dict]":
    """The proactive outbox's items for the file `stem` (one per body it was queued with).
    ValueError when any item record cannot be read: it could be this file's, so it proves nothing."""
    items = Path(result_dir) / RECEIPT_DIR / ITEMS_DIR
    for path in (items.glob("*.json") if items.is_dir() else ()):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"unreadable proactive outbox item {path.name}") from e
    return [rec for rec in list_items(Path(result_dir) / RECEIPT_DIR)
            if str(rec.get("item_id") or "").startswith(f"{stem}~")]


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
