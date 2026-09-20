#!/usr/bin/env python3
"""A concurrent merge must not be able to roll one of our elements backwards.

Yjs resolves two writers of the same key by CLIENT ID, which knows nothing about
element versions — so the peer with the higher client id wins even holding an
OLDER version, and the shape silently reverts for everyone. The web client
answers this by re-applying its scene after every remote change; this client has
to do the same or an agent's work is quietly undone by whoever it drew beside.

Run: python3 tests/room-doc-board-reconcile.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Map
except ImportError as exc:  # pragma: no cover
    print(f"room-doc board reconcile: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_doc_board import BOARD_KIND, ELEMENTS_KEY  # noqa: E402

from room_doc_client import RoomDoc  # noqa: E402
from room_doc_protocol import DEFAULT_TEXT_NAME  # noqa: E402

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def el(version, **over):
    base = {"id": "a", "type": "rectangle", "x": 0, "y": 0,
            "width": 10, "height": 10, "version": version}
    base.update(over)
    return base


def make():
    """The board, plus a record of what the observer actually re-asserted.

    Counting frames on the wire cannot see a needless re-assert — `is_newer`
    suppresses the write anyway — so the observer's own decisions are counted.
    """
    doc = Doc()
    board = RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=BOARD_KIND)
    board.reasserted = []
    inner = board._reassert

    async def spy(elements):
        board.reasserted.append([e["id"] for e in elements])
        return await inner(elements)

    board._reassert = spy
    return board, doc


async def remote_sets(ours: Doc, value: dict) -> None:
    """Apply a change made by another peer — no origin, like the real wire."""
    peer = Doc()
    peer.get(ELEMENTS_KEY, type=Map)
    peer.apply_update(ours.get_update())
    peer.get(ELEMENTS_KEY, type=Map)[value["id"]] = dict(value)
    ours.apply_update(peer.get_update(ours.get_state()))
    for _ in range(6):        # let the observer's ensure_future run
        await asyncio.sleep(0)


def check(name, coro):
    try:
        asyncio.run(coro())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


async def test_a_remote_older_version_is_answered_automatically():
    board, doc = make()
    await board.put_elements([el(5)])
    await remote_sets(doc, el(2))
    assert board.elements[0]["version"] == 5, (
        "a concurrent write rolled our element back to an older version and "
        "nothing re-asserted it")
    await board._stop()


async def test_a_remote_newer_version_is_left_alone():
    """Control: re-asserting must not mean always winning."""
    board, doc = make()
    await board.put_elements([el(5)])
    await remote_sets(doc, el(9))
    assert board.elements[0]["version"] == 9, "we fought a legitimately newer version"
    await board._stop()


async def test_an_untouched_element_of_ours_is_not_rewritten():
    board, doc = make()
    await board.put_elements([el(3, id="a"), el(3, id="b")])
    await remote_sets(doc, el(1, id="a"))
    assert board.elements[0]["version"] == 3
    # 'b' was not touched, so the re-assert must name 'a' alone.
    assert board.reasserted == [["a"]], f"re-asserted beyond the touched key: {board.reasserted}"
    await board._stop()


async def test_our_own_write_does_not_re_enter_the_observer():
    """Correctness here rests on is_newer, which suppresses the redundant write
    either way — so this asserts the observer's DECISION, not the wire."""
    board, _ = make()
    await board.put_elements([el(1)])
    for _ in range(6):
        await asyncio.sleep(0)
    assert board.reasserted == [], f"our own write fed the observer back: {board.reasserted}"
    await board._stop()


async def test_a_remote_change_to_something_we_never_wrote_is_ignored():
    board, doc = make()
    await board.put_elements([el(2, id="mine")])
    await remote_sets(doc, el(1, id="theirs"))
    assert board.reasserted == [], f"we re-asserted an element that is not ours: {board.reasserted}"
    assert {e["id"] for e in board.elements} == {"mine", "theirs"}
    await board._stop()


async def test_a_deletion_survives_a_concurrent_older_write():
    """Deleting is an ordinary newer write, so it must be defended the same way."""
    board, doc = make()
    await board.put_elements([el(4)])
    await board.delete_element("a")
    await remote_sets(doc, el(4))          # the pre-deletion version, re-sent
    stored = board.elements[0]
    assert stored.get("isDeleted") is True, "a concurrent write resurrected a deleted element"
    await board._stop()


async def test_reconcile_by_hand_re_asserts_what_this_session_wrote():
    """The manual door: no argument means everything this session claimed."""
    board, doc = make()
    await board.put_elements([el(4, id="a"), el(4, id="b")])
    peer = Doc()
    peer.get(ELEMENTS_KEY, type=Map)
    peer.apply_update(doc.get_update())
    pm = peer.get(ELEMENTS_KEY, type=Map)
    pm["a"] = el(1, id="a")
    pm["b"] = el(1, id="b")
    doc.apply_update(peer.get_update(doc.get_state()))
    for _ in range(6):
        await asyncio.sleep(0)
    assert await board.reconcile() == 0, "the observer already restored them"
    assert {e["version"] for e in board.elements} == {4}
    # and an explicit list is still honoured
    assert await board.reconcile([el(9, id="a")]) == 1
    await board._stop()


async def test_a_background_re_assert_after_the_session_ends_is_swallowed():
    """The observer fires from a callback; by then the socket may be gone, and
    an exception there has nowhere to go but the event loop."""
    board, _ = make()
    await board.put_elements([el(1)])
    board._ended.set_result(None)          # the session has ended
    await board._reassert([el(2)])         # must not raise
    await board._stop()


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc board reconcile: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc board reconcile: ok")
