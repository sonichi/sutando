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


async def test_the_library_is_read_only_by_bare_file_name():
    import room_collab
    for bad in ("../index.json", "x/y.html", "https://e/x.html", "a.js"):
        try:
            room_collab.fetch_library("http://127.0.0.1:9/", bad)
        except RoomDocError as e:
            assert "not a library file" in str(e), e
        else:
            raise AssertionError(f"{bad} must be refused before any request")


async def test_a_template_never_overwrites_a_page_unless_told_to():
    import argparse
    import json as _json
    import room_collab
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "html", kind=HTML_KIND)
    await page.append("<p>someone's work</p>")
    files = {"index.json": _json.dumps({"v": 1, "templates": [
        {"id": "blank", "name": "Blank", "file": "blank.html"}]}).encode(),
             "blank.html": b"<p>blank</p>"}
    real = room_collab.fetch_library
    room_collab.fetch_library = lambda base, name: files[name]
    try:
        args = argparse.Namespace(kind=HTML_KIND, library="http://lib/", use="blank",
                                  replace=False, json=False, settle=0)
        try:
            await room_collab.templates(page, args, "https://svc")
        except RoomDocError as e:
            assert "--replace" in str(e)
        else:
            raise AssertionError("must refuse to overwrite")
        assert page.text == "<p>someone's work</p>"
        args.replace = True
        assert await room_collab.templates(page, args, "https://svc") == 0
        assert page.text == "<p>blank</p>", page.text
    finally:
        room_collab.fetch_library = real


async def test_the_stage_is_shared_state_beside_the_page():
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "html", kind=HTML_KIND)
    first = await page.set_stage("step4")
    assert page.stage["topic"] == "step4" and page.stage["ts"] == first["ts"]
    assert str(doc.get("html", type=Text)) == "", "the stage never touches the page text"
    await page.set_stage(None)
    assert page.stage["topic"] == ""
    for bad in ("<img>", "a b", "", "x" * 65):
        try:
            await page.set_stage(bad or "-")
        except RoomDocError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be refused")
    board = RoomDoc(FakeWS(), Doc(), Awareness(Doc()), "markdown", kind="board")
    try:
        await board.set_stage("x")
    except RoomDocError:
        pass
    else:
        raise AssertionError("only the HTML page has a stage")


async def test_the_page_state_is_shared_and_bounded_like_the_web_client():
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "html", kind=HTML_KIND)
    await page.set_app_state("votes", {"u1": "Room-collab"})
    assert page.app_state == {"votes": {"u1": "Room-collab"}}
    assert str(doc.get("html", type=Text)) == "", "state never touches the page text"
    for key, value in (("../x", 1), ("big", "x" * 5000)):
        try:
            await page.set_app_state(key, value)
        except RoomDocError:
            pass
        else:
            raise AssertionError(f"{key} must be refused")
    await page.set_app_state("votes", None)
    assert page.app_state == {}


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab html surface: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab html surface: 7 passed")
