#!/usr/bin/env python3
"""The shared stage on the board and the Doc, and the relay that switches between surfaces.

The failures worth pinning: the agent's frame or heading numbering drifting
from the web client's (a "go to 3" landing on another part), a move written to
a surface that has no stage, and the relay acting on the surface it held
before a switch.

Run: python3 tests/room-collab-surface-stage.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab surface stage: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_board import complete_element  # noqa: E402
from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import DEFAULT_KIND, HTML_KIND, RoomDocError  # noqa: E402
from room_collab_relay import route, serve  # noqa: E402
from surface_outline import board_outline, doc_headings, slide_frames  # noqa: E402

FAILS = []
PASSED = []


def check(name, fn):
    try:
        asyncio.run(fn())
        PASSED.append(name)
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


class FakeWS:
    async def send(self, payload):
        pass


def frame(i, x, y, name=None, **extra):
    return {"id": i, "type": "frame", "x": x, "y": y, "width": 100, "height": 60, "version": 1,
            **({"name": name} if name else {}), **extra}


def text(i, words, **extra):
    return {"id": i, "type": "text", "x": 0, "y": 0, "width": 10, "height": 10, "version": 1,
            "text": words, **extra}


# The same fixtures as the web client's boardStage.smoke.ts and docStage.smoke.ts.
SCENE = [frame("a", 0, 200, "Risks"), frame("b", 200, 0), frame("c", 0, 0, "Intro")]
DOC = "\n".join(["# Plan", "", "Intro text.", "## Goals ##", "```md", "# not a heading", "```",
                 "#nospace", "#", "   ### Risks", "The risks of", "shipping late."])


async def test_frames_are_numbered_in_the_clients_present_order():
    assert [f["id"] for f in slide_frames(SCENE)] == ["c", "b", "a"]
    tall = [frame("tall", 0, 0, height=400), frame("beside", 200, 300), frame("under", 0, 500)]
    assert [f["id"] for f in slide_frames(tall)] == ["tall", "beside", "under"]
    gone = [frame("live", 0, 0), frame("gone", 200, 0, isDeleted=True), frame("flat", 400, 0, width=0)]
    assert [f["id"] for f in slide_frames(gone)] == ["live"]


async def test_the_board_outline_lists_each_frames_words():
    els = SCENE + [text("t1", "Ship  late?", frameId="a"),
                   {"id": "box", "type": "rectangle", "x": 0, "y": 0, "width": 5, "height": 5, "frameId": "c"},
                   text("t2", "Hello", containerId="box"),
                   text("t3", "gone", frameId="c", isDeleted=True)]
    o = board_outline(els)
    assert o == {"kind": "board", "slides": [
        {"n": 1, "title": "Intro", "texts": ["Hello"]},
        {"n": 2, "title": None, "texts": []},
        {"n": 3, "title": "Risks", "texts": ["Ship late?"]}]}, o


async def test_doc_headings_follow_the_clients_rule():
    assert [(h["level"], h["title"], h["line"]) for h in doc_headings(DOC)] == [
        (1, "Plan", 1), (2, "Goals", 4), (3, "Risks", 10)]


async def test_moves_and_spots_land_on_the_board_and_doc_stages():
    for kind, name in (("board", "markdown"), (DEFAULT_KIND, "markdown")):
        d = Doc()
        surface = RoomDoc(FakeWS(), d, Awareness(d), name, kind=kind)
        nav = await surface.navigate("goto", 2)
        assert surface.stage["nav"] == nav and nav["n"] == 2, (kind, surface.stage)
        first = await surface.set_spot("  the  risks ")
        second = await surface.set_spot(None)
        assert first["text"] == "the risks" and second["text"] == ""
        assert second["seq"] > first["seq"], "a spot is re-taken only when its seq rises"
        try:
            await surface.set_stage("topic")
        except RoomDocError:
            pass
        else:
            raise AssertionError(f"topic highlights are the page's, not the {kind}'s")
    d = Doc()
    kanban = RoomDoc(FakeWS(), d, Awareness(d), "markdown", kind="kanban")
    try:
        await kanban.navigate("next")
    except RoomDocError:
        pass
    else:
        raise AssertionError("the kanban has no stage")


async def http(port, method, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_surface_routes():
    assert route("POST", "/surface/board") == ("surface", "board")
    assert route("POST", "/surface/Doc") == ("surface", "doc")
    assert route("GET", "/surface") == ("surface", None)
    assert route("POST", "/surface/kanban")[0] == 400
    assert route("GET", "/surface/board")[0] == 405


async def test_the_relay_switches_surface_and_acts_on_the_one_it_holds():
    def opened(kind, name):
        d = Doc()
        s = RoomDoc(FakeWS(), d, Awareness(d), name, kind=kind)
        s.closed = lambda: asyncio.sleep(3600)
        return s

    page = opened(HTML_KIND, "html")
    board = opened("board", "markdown")
    doc = opened(DEFAULT_KIND, "markdown")
    await board.put_elements([complete_element(e) for e in SCENE + [text("t", "Ship late", frameId="a")]])
    doc._text.insert(0, DOC)
    opens = []

    @asynccontextmanager
    async def open_doc(room):
        opens.append("html")
        yield page

    @asynccontextmanager
    async def open_kind(room, kind):
        opens.append(kind)
        yield {"board": board, DEFAULT_KIND: doc}[kind]

    port = 47823
    task = asyncio.create_task(serve(open_doc, port, room="!a:x", log=lambda *_: None, open_kind=open_kind))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(port, "GET", "/surface")
        assert body == {"ok": True, "surface": "html", "connected": True, "parts": 0}, body
        status, body = await http(port, "POST", "/surface/board")
        assert status == 200 and body["surface"] == "board" and body["parts"] == 3, body
        status, body = await http(port, "GET", "/outline")
        assert [s["title"] for s in body["slides"]] == ["Intro", None, "Risks"], body
        status, body = await http(port, "POST", "/slide/3")
        assert status == 200 and board.stage["nav"]["n"] == 3, body
        assert "nav" not in page.stage, "a move after the switch never reaches the page"
        status, body = await http(port, "POST", "/slide/4")
        assert status == 400 and "3 frames" in body["error"], body
        status, body = await http(port, "POST", "/spot/ship%20LATE")
        assert body["found_on_page"] is True and board.stage["spot"]["text"] == "ship LATE", body
        status, body = await http(port, "POST", "/highlight/step1")
        assert status == 409, body
        status, body = await http(port, "GET", "/state")
        assert body["surface"] == "board", body

        status, body = await http(port, "POST", "/surface/doc")
        assert body["surface"] == "doc" and body["parts"] == 3, body
        status, body = await http(port, "GET", "/outline")
        assert [h["title"] for h in body["headings"]] == ["Plan", "Goals", "Risks"], body
        status, body = await http(port, "POST", "/spot/risks%20of%20shipping")
        assert body["found_on_page"] is True and doc.stage["spot"]["seq"] > 0, body
        status, body = await http(port, "POST", "/slide/next")
        assert doc.stage["nav"]["cmd"] == "next", doc.stage

        status, body = await http(port, "POST", "/surface/html")
        assert body["surface"] == "html", body
        status, body = await http(port, "GET", "/state")
        assert set(body) == {"topic", "ts", "speaking", "room"}, "the page's /state: no surface key"
        assert opens == ["html", "board", DEFAULT_KIND, "html"], opens
    finally:
        task.cancel()


async def test_without_other_surfaces_the_relay_says_so():
    page = RoomDoc(FakeWS(), Doc(), Awareness(Doc()), "html", kind=HTML_KIND)
    page.closed = lambda: asyncio.sleep(3600)

    @asynccontextmanager
    async def open_doc(room):
        yield page

    task = asyncio.create_task(serve(open_doc, 47824, room="!a:x", log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47824, "POST", "/surface/board")
        assert status == 503 and "other surfaces" in body["error"], body
        status, body = await http(47824, "POST", "/surface/html")
        assert status == 200 and body["surface"] == "html", body
    finally:
        task.cancel()


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab surface stage: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"room-collab surface stage: {len(PASSED)} passed")
