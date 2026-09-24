#!/usr/bin/env python3
"""The board half of the room-collab client, against a real pycrdt document.

The behaviour under test is a REFUSAL, and refusals are what this client keeps
getting wrong in the same way: before this, opening the board and asking for
its text returned `""` — identical to an empty whiteboard — and appending text
to it SUCCEEDED, writing a markdown Y.Text into the board that no Excalidraw
client ever reads. Both are silent. Both are pinned here.

No socket: the client is constructed directly around a stand-in that records
frames, so this exercises the shipped methods without a server.
Run: python3 tests/room-collab-board-client.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import inspect
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "room-collab"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab board client: FAIL — dependencies missing ({exc}).")
    print("  CI installs skills/room-collab/requirements.txt; a skip here would")
    print("  make this suite green without running, which is the bug it guards.")
    sys.exit(1)

from room_collab_board import BOARD_KIND  # noqa: E402
from room_collab_client import RoomDoc  # noqa: E402

from room_collab_protocol import DEFAULT_KIND, DEFAULT_TEXT_NAME, RoomDocError  # noqa: E402

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make(kind):
    doc = Doc()
    return RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=kind)


def el(**over):
    base = {"id": "a", "type": "rectangle", "x": 0, "y": 0,
            "width": 10, "height": 10, "version": 1}
    base.update(over)
    return base


def check(name, coro_fn):
    try:
        asyncio.run(coro_fn())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


async def expect_refusal(fn, *must_mention):
    """`fn` may return a value or a coroutine; an un-awaited coroutine would
    swallow the refusal and read as "it did not raise"."""
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
    except RoomDocError as exc:
        for token in must_mention:
            assert token in str(exc), f"the refusal must mention {token!r}: {exc}"
        return
    raise AssertionError(f"expected a refusal, got {result!r}")


async def test_reading_text_on_a_board_refuses_instead_of_returning_empty():
    board = make(BOARD_KIND)
    await expect_refusal(lambda: board.text, "board", "elements")


async def test_writing_text_to_a_board_refuses_instead_of_succeeding():
    board = make(BOARD_KIND)
    await expect_refusal(lambda: board.append("hello"), "board")
    await expect_refusal(lambda: board.insert(0, "hello"), "board")
    await expect_refusal(lambda: board.replace("a", "b"), "board")
    assert board._ws.sent == [], "a refused write must put nothing on the wire"


async def test_the_markdown_document_still_reads_and_writes_text():
    """Control: the refusal is about the board, not about every document."""
    doc = make(DEFAULT_KIND)
    assert doc.text == ""
    await doc.append("hello")
    assert doc.text == "hello"
    assert doc._ws.sent, "a real write must reach the wire"


async def test_elements_on_a_markdown_document_refuse():
    doc = make(DEFAULT_KIND)
    await expect_refusal(lambda: doc.elements, "board")


async def test_a_written_element_reads_back():
    board = make(BOARD_KIND)
    assert await board.put_elements([el()]) == 1
    assert [e["id"] for e in board.elements] == ["a"]


async def test_a_minimal_element_is_stored_complete():
    """The editor reads groupIds on EVERY element when anything is selected,
    so one minimal element anywhere makes the whole board unselectable. The
    writer fills what the editor's own restore step would."""
    board = make(BOARD_KIND)
    await board.put_elements([el()])
    stored = board.elements[0]
    for key in ("groupIds", "boundElements", "strokeColor", "seed", "versionNonce", "updated"):
        assert key in stored, f"{key} missing from the stored element"
    assert stored["groupIds"] == []
    # Re-asserting the same minimal element inherits the stored identity, so
    # the version tie-break has nothing random to decide on.
    before = len(board._ws.sent)
    assert await board.put_elements([el()]) == 0
    assert len(board._ws.sent) == before


async def test_an_invalid_element_is_refused_and_nothing_is_written():
    board = make(BOARD_KIND)
    await expect_refusal(lambda: board.put_elements([el(), el(id="b", type="iframe")]),
                         "board element")
    assert board.elements == [], "a partial write is worse than none"


async def test_an_older_element_is_not_written():
    board = make(BOARD_KIND)
    await board.put_elements([el(version=5)])
    assert await board.put_elements([el(version=4)]) == 0
    assert board.elements[0]["version"] == 5


async def test_re_applying_an_unchanged_element_writes_nothing():
    """Every write is an update every peer receives and persists."""
    board = make(BOARD_KIND)
    await board.put_elements([el(version=2)])
    before = len(board._ws.sent)
    assert await board.put_elements([el(version=2)]) == 0
    assert len(board._ws.sent) == before, "an unchanged scene must not reach the wire"


async def test_delete_marks_deleted_and_bumps_the_version():
    board = make(BOARD_KIND)
    await board.put_elements([el(version=3)])
    await board.delete_element("a")
    stored = board.elements[0]
    assert stored["isDeleted"] is True
    assert stored["version"] == 4, "a deletion is an ordinary newer write"
    assert board.live_elements == [], "and it leaves the canvas"


async def test_deleting_an_absent_element_refuses():
    board = make(BOARD_KIND)
    await expect_refusal(lambda: board.delete_element("nope"), "no such element")



async def test_every_non_markdown_kind_refuses_text_not_just_the_board():
    """The board fix named ONE kind, so kanban inherited the whole defect:
    `read` answered "" and `append` succeeded, writing text into a board of
    cards. A kind nobody has built yet must refuse too — the next surface
    should not have to rediscover this."""
    for kind in ("kanban", "slides", "sheet", "anything-later"):
        doc = make(kind)
        await expect_refusal(lambda d=doc: d.text, kind)
        await expect_refusal(lambda d=doc: d.append("x"), kind)
        assert doc._ws.sent == [], f"{kind}: a refused write reached the wire"


async def test_the_markdown_document_is_still_the_one_text():
    """Control: inverting the check must not refuse the document that IS text."""
    doc = make(DEFAULT_KIND)
    assert doc.text == ""
    await doc.append("hello")
    assert doc.text == "hello"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab board client: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab board client: ok")
