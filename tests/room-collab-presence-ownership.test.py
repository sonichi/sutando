#!/usr/bin/env python3
"""This client must never re-broadcast another peer's presence.

The room-collab server treats any channel that CARRIES an awareness update for a
client id as an owner of that id, and only drops the peer when no owner is left
(`_owners[client_id] = {channels}`). So a client that forwards remote awareness
makes every other participant immortal: they stay in the roster after they
disconnect, until this socket also closes.

It gets worse with scale, which is why it matters for a multi-seat agent: with
N such clients in one document, every one of them holds every other's id, so no
peer can ever leave while any remains.

Run: python3 tests/room-collab-presence-ownership.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc, YMessageType
except ImportError as exc:  # pragma: no cover
    print(f"room-collab presence ownership: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402

from room_collab_protocol import DEFAULT_KIND, DEFAULT_TEXT_NAME  # noqa: E402

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def awareness_ids(frame: bytes) -> list[int]:
    """The client ids an awareness frame speaks for, read off the wire."""
    body = frame[1:]
    pos = 0

    def var_uint():
        nonlocal pos
        result = shift = 0
        while True:
            byte = body[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7

    var_uint()                      # payload length
    count = var_uint()
    return [var_uint() for _ in range(count)] if count else []


def make():
    doc = Doc()
    aw = Awareness(doc)
    rd = RoomDoc(FakeWS(), doc, aw, DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)
    rd._start_presence_renewal()
    return rd, aw


def check(name, coro):
    try:
        asyncio.run(coro())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")
    finally:
        pass


async def test_a_remote_peers_presence_is_never_re_sent():
    rd, aw = make()
    peer_doc = Doc()
    peer = Awareness(peer_doc)
    peer.set_local_state_field("user", {"name": "someone else"})

    aw.apply_awareness_update(peer.encode_awareness_update([peer.client_id]), "remote")
    await asyncio.sleep(0)

    for frame in rd._ws.sent:
        if frame and frame[0] == YMessageType.AWARENESS:
            assert peer.client_id not in awareness_ids(frame), (
                "this socket re-sent a remote peer's id and now owns it server-side; "
                "that peer can never leave the roster")
    await rd._stop()


async def test_our_own_presence_is_still_published():
    """Control: the fix must not silence us — a peer that stops renewing EXPIRES."""
    rd, aw = make()
    await rd.set_presence("mars", user_id="@mars:ag2space.local")
    await asyncio.sleep(0)

    mine = [f for f in rd._ws.sent
            if f and f[0] == YMessageType.AWARENESS and aw.client_id in awareness_ids(f)]
    assert mine, "our own presence must reach the wire, or the server expires us"
    await rd._stop()


async def test_a_remote_update_does_not_suppress_our_own_later_update():
    """The guard is on origin, not a latch: presence must keep working after a
    remote peer has been seen."""
    rd, aw = make()
    peer_doc = Doc()
    peer = Awareness(peer_doc)
    peer.set_local_state_field("user", {"name": "other"})
    aw.apply_awareness_update(peer.encode_awareness_update([peer.client_id]), "remote")
    await asyncio.sleep(0)
    before = len(rd._ws.sent)

    await rd.set_presence("mars-again")
    await asyncio.sleep(0)
    assert len(rd._ws.sent) > before, "our presence stopped being published"
    await rd._stop()


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab presence ownership: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab presence ownership: ok")
