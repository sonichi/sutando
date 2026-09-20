#!/usr/bin/env python3
"""A held document connection turns remote edits into "a new line names me".

Without this an agent connected to a document sees characters change and
nothing else; a person has to ping it in the room. Two halves: the pure rules
that find new lines and the ones addressed to a handle, and the client's
`changes()` iterator that feeds them from a live document.

Run: python3 tests/room-doc-watch.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-doc watch: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_doc_client import RoomDoc  # noqa: E402
from room_doc_protocol import DEFAULT_KIND, DEFAULT_TEXT_NAME, RoomDocError  # noqa: E402

from room_doc_watch import addressed_to, new_lines  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


# --- the pure rules

def test_new_lines_are_the_ones_that_appeared():
    assert new_lines("a\nb", "a\nb\nc") == ["c"]
    assert new_lines("a\nb", "z\na\nb") == ["z"], "position does not matter, presence does"
    assert new_lines("a", "a") == []


def test_a_line_written_a_second_time_is_new_again():
    assert new_lines("todo: x", "todo: x\ntodo: x") == ["todo: x"]


def test_blank_lines_are_never_new():
    assert new_lines("a", "a\n\n\n") == []


def test_addressed_to_matches_the_at_mention_as_a_whole_token():
    lines = ["@mars please look", "@marshall must not match", "for mars, not a mention",
             "cc @Mars in caps", "@sutando-qingyun-001:ag2.space by mxid", "@mars."]
    got = addressed_to(lines, ["mars", "@sutando-qingyun-001:ag2.space"])
    assert got == ["@mars please look", "cc @Mars in caps",
                   "@sutando-qingyun-001:ag2.space by mxid", "@mars."], got


def test_cjk_and_spaces_in_a_handle_match_literally():
    assert addressed_to(["@小火星 看一下", "@Mars the product dev: hi"],
                        ["小火星", "Mars the product dev"]) == \
        ["@小火星 看一下", "@Mars the product dev: hi"]


def test_no_handles_means_no_lines_not_all_lines():
    assert addressed_to(["@a", "@b"], []) == []


# --- the live half: changes() on a document edited by a remote peer

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make():
    doc = Doc()
    doc.get(DEFAULT_TEXT_NAME, type=Text)
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)


def remote_append(local: Doc, text: str) -> None:
    """What a peer's edit looks like when it arrives: an update applied to
    our doc with no local origin."""
    peer = Doc()
    peer.apply_update(local.get_update())
    peer.get(DEFAULT_TEXT_NAME, type=Text).__iadd__(text)
    local.apply_update(peer.get_update(local.get_state()))


async def test_a_remote_edit_is_yielded_as_the_new_text():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    remote_append(doc, "hello @mars")
    got = await asyncio.wait_for(nxt, 2)
    assert got == "hello @mars", got
    await agen.aclose()


async def test_the_agents_own_write_is_not_reported():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    await room.append("mine")            # local origin
    remote_append(doc, " theirs")       # then a real remote edit
    got = await asyncio.wait_for(nxt, 2)
    assert got == "mine theirs", "only the remote edit should wake the watcher"
    assert nxt.done()
    await agen.aclose()


async def test_the_session_ending_raises_instead_of_stopping_quietly():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    room._ended.set_result(None)
    try:
        await asyncio.wait_for(nxt, 2)
    except RoomDocError as e:
        assert "ended" in str(e)
    else:
        raise AssertionError("a closed session must raise, not look like silence")


async def test_closing_the_iterator_leaves_the_session_alive():
    """The first version waited on the session future directly, so closing
    the iterator cancelled the session and the next write raised."""
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    nxt.cancel()
    try:
        await nxt
    except asyncio.CancelledError:
        pass
    await agen.aclose()
    assert not room._ended.done(), "closing the watcher must not end the session"
    await room.append("still writable")
    assert room.text == "still writable"


async def test_settle_turns_a_burst_of_keystrokes_into_one_text():
    """Measured on production: one push per keystroke, no batching. Without
    coalescing a watcher prints the mention line once per character typed."""
    doc, room = make()
    agen = room.changes(settle=0.15)
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    for ch in "@mars hi":                       # eight pushes, 20 ms apart
        remote_append(doc, ch)
        await asyncio.sleep(0.02)
    assert not nxt.done(), "nothing should be yielded while pushes keep coming"
    got = await asyncio.wait_for(nxt, 2)
    assert got == "@mars hi", got
    # and nothing else is queued behind it
    nxt2 = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.3)
    assert not nxt2.done(), "the burst must yield exactly once"
    nxt2.cancel()
    try:
        await nxt2
    except asyncio.CancelledError:
        pass
    await agen.aclose()


async def test_without_settle_every_push_is_yielded():
    doc, room = make()
    agen = room.changes()
    got = []
    async def take(n):
        async for text in agen:
            got.append(text)
            if len(got) == n:
                break
    task = asyncio.ensure_future(take(3))
    await asyncio.sleep(0)
    for ch in "abc":
        remote_append(doc, ch)
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, 2)
    assert got == ["a", "ab", "abc"], got
    await agen.aclose()


async def test_new_lines_through_changes_find_the_mention():
    """The whole path a watcher runs: snapshot, remote edit, diff, address."""
    doc, room = make()
    seen = room.text
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    remote_append(doc, "line one\n@mars line two\n@other line three")
    text = await asyncio.wait_for(nxt, 2)
    assert addressed_to(new_lines(seen, text), ["mars"]) == ["@mars line two"]
    await agen.aclose()


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc watch: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc watch: ok")
