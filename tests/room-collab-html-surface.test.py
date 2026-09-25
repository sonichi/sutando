#!/usr/bin/env python3
"""The HTML page is a text surface, edited with the Doc's own text commands.

It lives under its own root on its own `?kind=html` document, so a write to it
must never land in the markdown Doc, and the Doc must not start reading it.

Run: python3 tests/room-collab-html-surface.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-collab html surface: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import (DEFAULT_KIND, HTML_KIND, TEXT_ROOTS, RoomDocError,  # noqa: E402
                                  doc_socket_url)

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def check(name, fn):
    try:
        asyncio.run(fn())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


async def test_html_is_a_text_kind_under_its_own_root():
    assert TEXT_ROOTS[HTML_KIND] == "html" and TEXT_ROOTS[DEFAULT_KIND] == "markdown"
    assert doc_socket_url("https://x", "!r:x", kind=HTML_KIND).endswith("kind=html")


async def test_an_append_lands_in_the_html_root_and_nowhere_else():
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), TEXT_ROOTS[HTML_KIND], kind=HTML_KIND)
    await page.append("<h1>Mockup</h1>")
    assert str(doc.get("html", type=Text)) == "<h1>Mockup</h1>"
    assert str(doc.get("markdown", type=Text)) == ""
    assert page.text == "<h1>Mockup</h1>" and page.snapshot()["text"] == "<h1>Mockup</h1>"
    assert page._ws.sent, "the edit went out as an update"


async def test_a_structured_kind_is_still_refused_as_text():
    doc = Doc()
    other = RoomDoc(FakeWS(), doc, Awareness(doc), "markdown", kind="board")
    try:
        await other.append("x")
    except RoomDocError as e:
        assert "'markdown', 'html'" in str(e), e
    else:
        raise AssertionError("a board must refuse a text write")


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab html surface: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab html surface: 3 passed")
