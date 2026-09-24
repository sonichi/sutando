#!/usr/bin/env python3
"""Replacing text in a document that contains non-ASCII must not eat its words.

pycrdt indexes a Text by UTF-8 BYTES; `str.find` counts CHARACTERS. Where the
two disagree — any document with CJK, accents or emoji ahead of the match — the
delete lands at the wrong offset and removes somebody else's sentence. An
ASCII-only case cannot see this: the two indexes coincide exactly.

This is not hypothetical. The unfixed code was run against a live room document
whose text was Chinese and silently deleted seven characters out of the middle
of a sentence the agent had not been asked to touch.

Pure: a document and a fake socket, no server.
Run: python3 tests/room-collab-replace-offsets.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "room-collab"))

try:
    from pycrdt import Awareness, Doc, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-collab replace offsets: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import DEFAULT_KIND, DEFAULT_TEXT_NAME  # noqa: E402

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make(initial: str) -> RoomDoc:
    doc = Doc()
    text = doc.get(DEFAULT_TEXT_NAME, type=Text)
    room = RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)
    if initial:
        text += initial
    return room


def check(name, fn):
    try:
        asyncio.run(fn())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


async def test_a_real_document_is_not_silently_mangled():
    """The exact shape that damaged a live document, and the reason this matters
    more than the crash cases: within bounds the bad offset does not raise. It
    removes a different, perfectly valid span and reports success."""
    body = ("@sutando-qingyun-001 Mars-the-product-dev\n\n"
            "现在 @ 的时候，系统没有显示可以选择的 at 的人，所以我要手动拼写这个 agent，这个非常不方便")
    room = make(body + "STRAY)")
    await room.replace("STRAY)", "")
    assert room.text == body, (
        "the wrong span was deleted; the document now reads:\n  " + room.text)
    assert "的 at 的人，" in room.text, "her words were eaten"


async def test_a_trailing_ascii_match_after_cjk_is_removed_exactly():
    """The shape that corrupted a real document: ASCII stray, CJK in front."""
    body = "现在 @ 的时候，系统没有显示可以选择的 at 的人，所以我要手动拼写"
    room = make(body + "STRAY")
    await room.replace("STRAY", "")
    assert room.text == body, f"the CJK sentence was altered: {room.text!r}"


async def test_replacement_lands_where_the_match_is_not_where_bytes_drift():
    room = make("题目：alpha 和 beta")
    await room.replace("alpha", "ALPHA")
    assert room.text == "题目：ALPHA 和 beta", room.text


async def test_an_emoji_before_the_match_shifts_by_four_bytes():
    """Emoji are 4 UTF-8 bytes; a 1-char/4-byte gap is the widest common case."""
    room = make("🎉 done: old")
    await room.replace("old", "new")
    assert room.text == "🎉 done: new", room.text


async def test_the_match_itself_may_be_non_ascii():
    room = make("prefix 中文中文 suffix")
    await room.replace("中文中文", "X")
    assert room.text == "prefix X suffix", room.text


async def test_ascii_only_still_works():
    """The control: the case that passed before must not regress."""
    room = make("hello world")
    await room.replace("world", "there")
    assert room.text == "hello there", room.text


async def test_absent_text_is_refused_not_silently_skipped():
    room = make("中文 body")
    try:
        await room.replace("nothing like this", "x")
    except Exception as e:  # noqa: BLE001
        assert "not in the document" in str(e), e
        return
    raise AssertionError("replacing absent text must raise, not write nothing")


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab replace offsets: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab replace offsets: ok")
