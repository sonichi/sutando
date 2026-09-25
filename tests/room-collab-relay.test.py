#!/usr/bin/env python3
"""The stage relay: a voice agent's local talk-highlight calls land on the room's page.

The failures worth pinning: a malformed topic reaching the stage, `speaking`
re-stamping the highlight (a deck would re-run it), and the port answering
before the page is connected as though a write had happened.

Run: python3 tests/room-collab-relay.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import inspect
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab relay: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import HTML_KIND  # noqa: E402
from room_collab_relay import route, serve  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        asyncio.run(fn())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


class FakeWS:
    async def send(self, payload):
        pass


async def test_routes():
    assert route("POST", "/highlight/Step4") == ("highlight", "step4")
    assert route("POST", "/highlight/clear") == ("highlight", None)
    assert route("POST", "/highlight/<x>")[0] == 400
    assert route("POST", "/speaking/on") == ("speaking", True)
    assert route("GET", "/state?x=1") == ("state", None)
    assert route("GET", "/script") == ("script", None)
    assert route("GET", "/outline") == ("outline", None)
    assert route("POST", "/spot/Liveness%20isn%E2%80%99t%20health") == ("spot", "Liveness isn\u2019t health")
    assert route("POST", "/spot/clear") == ("spot", None) and route("POST", "/spot/")[0] >= 400
    assert route("GET", "/highlight/x")[0] == 405
    assert route("POST", "/deck/other")[0] == 404
    assert route("POST", "/presenter/on")[0] == 200
    assert route("POST", "/slide/next") == ("slide", ("next", None))
    assert route("POST", "/slide/12") == ("slide", ("goto", 12))
    assert route("POST", "/slide/0")[0] == 400 and route("POST", "/slide/up")[0] == 400
    assert route("POST", "/room/%21abc%3Aexample.org") == ("room", "!abc:example.org")
    assert route("GET", "/rooms") == ("rooms", None)
    for bad in ("/room/abc", "/room/%21abc", "/room/%21a%20b%3Ax", "/room/%21a%3Ab%2F..%2Fc"):
        assert route("POST", bad)[0] == 400, bad
    assert route("POST", "/room/")[0] >= 400
    assert route("GET", "/room/%21abc%3Ax")[0] == 405


async def http(port, method, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_a_round_trip_lands_on_the_stage_and_speaking_leaves_the_highlight():
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "html", kind=HTML_KIND)
    page.closed = lambda: asyncio.sleep(3600)

    @asynccontextmanager
    async def open_doc(room):
        yield page

    task = asyncio.create_task(serve(open_doc, 47811, room="!a:x", log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47811, "POST", "/highlight/step4")
        assert status == 200 and body["topic"] == "step4", body
        ts = page.stage["ts"]
        status, _ = await http(47811, "POST", "/speaking/on")
        assert status == 200 and page.stage["speaking"] is True
        assert page.stage["ts"] == ts, "speaking must not re-stamp the highlight"
        status, body = await http(47811, "GET", "/state")
        assert body == {"topic": "step4", "ts": ts, "speaking": True, "room": "!a:x"}, body
        status, body = await http(47811, "POST", "/slide/next")
        first = body["seq"]
        status, body = await http(47811, "POST", "/slide/3")
        assert status == 200 and body["cmd"] == "goto" and body["n"] == 3 and body["seq"] > first, body
        page._text.insert(0, "<section class=slide><h1>Loop</h1><p data-topic=trust>Trust &amp; safety</p></section>")
        status, body = await http(47811, "POST", "/spot/trust%20%26%20safety")
        assert status == 200 and body["found_on_page"] is True and page.stage["spot"]["text"] == "trust & safety", body
        status, body = await http(47811, "POST", "/spot/not%20here")
        assert body["found_on_page"] is False, body
        status, body = await http(47811, "GET", "/outline")
        assert body["kind"] == "deck" and body["slides"][0]["topics"][0]["topic"] == "trust", body
        status, body = await http(47811, "POST", "/highlight/%3Cx%3E")
        assert status == 400, body
    finally:
        task.cancel()


def fake_page(text=""):
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "html", kind=HTML_KIND)
    if text:
        page._text.insert(0, text)
    page.ended = asyncio.Event()

    async def closed():
        await page.ended.wait()
    page.closed = closed
    return page


async def test_a_switch_drops_the_old_room_and_every_call_after_lands_on_the_new_one():
    pages = {"!a:x": fake_page("<p>A</p>"), "!b:x": fake_page(), "!c:x": fake_page("<p>C</p>")}
    opened, texts, exited = [], [], []

    @asynccontextmanager
    async def open_doc(room):
        opened.append(room)
        try:
            yield pages[room]
        finally:
            exited.append(room)

    class Text:
        text = "# Talk script\n\nHello there."

    @asynccontextmanager
    async def open_text(room):
        texts.append(room)
        yield Text()

    task = asyncio.create_task(serve(open_doc, 47812, room="!a:x", open_text=open_text,
                                     list_rooms=lambda: [{"id": "!a:x", "name": "A"}],
                                     log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47812, "GET", "/state")
        assert body["room"] == "!a:x" and opened == ["!a:x"], (body, opened)
        status, body = await http(47812, "POST", "/room/%21b%3Ax")
        assert status == 200 and body == {"ok": True, "room": "!b:x", "connected": True,
                                          "has_page": False, "chars": 0}, body
        assert opened == ["!a:x", "!b:x"] and exited == ["!a:x"], (opened, exited)
        status, body = await http(47812, "POST", "/highlight/step1")
        assert pages["!b:x"].stage.get("topic") == "step1" and not pages["!a:x"].stage.get("topic")
        status, body = await http(47812, "GET", "/script")
        assert status == 200 and texts == ["!b:x"], (body, texts)
        status, body = await http(47812, "POST", "/room/%21b%3Ax")
        assert body["connected"] and opened == ["!a:x", "!b:x"], "the held room is not reopened"
        await http(47812, "POST", "/room/%21c%3Ax")
        status, body = await http(47812, "GET", "/state")
        assert body["room"] == "!c:x", body
        pages["!c:x"].ended.set()  # the connection drops: the relay reconnects to the same room
        pages["!c:x"].ended = asyncio.Event()
        await asyncio.sleep(1.3)
        assert opened[-2:] == ["!c:x", "!c:x"], opened
        status, body = await http(47812, "POST", "/room/not-a-room")
        assert status == 400 and opened[-1] == "!c:x", body
    finally:
        task.cancel()


async def test_a_room_that_cannot_open_reports_so_and_the_next_switch_still_lands():
    from room_collab_protocol import RoomDocError
    import room_collab_relay
    room_collab_relay.SWITCH_WAIT_S = 0.5
    good = fake_page("<p>ok</p>")

    @asynccontextmanager
    async def open_doc(room):
        if room == "!gone:x":
            raise RoomDocError("not a member of that room")
        yield good

    task = asyncio.create_task(serve(open_doc, 47813, room="!ok:x", log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47813, "POST", "/room/%21gone%3Ax")
        assert status == 200 and body["connected"] is False and "not a member" in body["error"], body
        status, body = await http(47813, "POST", "/room/%21ok%3Ax")
        assert body["connected"] is True and body["has_page"] is True, body
    finally:
        task.cancel()
        room_collab_relay.SWITCH_WAIT_S = 8


async def test_rooms_lists_through_the_injected_lookup_and_names_the_current_room():
    from room_collab_protocol import RoomDocError
    calls = []

    def list_rooms():
        calls.append(1)
        return [{"id": "!a:x", "name": "Qingyun Group"}, {"id": "!b:x", "name": None}]

    @asynccontextmanager
    async def open_doc(room):
        yield fake_page()

    task = asyncio.create_task(serve(open_doc, 47814, room="!a:x", list_rooms=list_rooms,
                                     log=lambda *_: None))
    bare = asyncio.create_task(serve(open_doc, 47815, room="!a:x", log=lambda *_: None))

    def failing():
        raise RoomDocError("could not list rooms: no gateway configured")
    broken = asyncio.create_task(serve(open_doc, 47816, room="!a:x", list_rooms=failing,
                                       log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47814, "GET", "/rooms")
        assert status == 200 and body == {"ok": True, "current": "!a:x", "rooms": [
            {"id": "!a:x", "name": "Qingyun Group"}, {"id": "!b:x", "name": None}]}, body
        status, body = await http(47815, "GET", "/rooms")
        assert status == 503 and "room list" in body["error"], body
        status, body = await http(47816, "GET", "/rooms")
        assert status == 503 and "no gateway" in body["error"], body
    finally:
        for t in (task, bare, broken):
            t.cancel()


def test_joined_rooms_reads_room_ops_output():
    import subprocess
    from room_collab import joined_rooms
    from room_collab_protocol import RoomDocError

    def runner(out, code=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, "")
    ok = json.dumps({"ok": True, "rooms": ["!a:x", "!b:x"],
                     "rooms_detailed": [{"room_id": "!a:x", "name": "Qingyun Group"}]})
    got = joined_rooms(runner=runner(ok), script=Path("room_ops.py"))
    assert got == [{"id": "!a:x", "name": "Qingyun Group"}, {"id": "!b:x", "name": None}], got
    for bad in (json.dumps({"ok": False, "reason": "no gateway configured"}), "boom"):
        try:
            joined_rooms(runner=runner(bad, 1), script=Path("room_ops.py"))
        except RoomDocError as exc:
            assert "could not list rooms" in str(exc)
        else:
            raise AssertionError("a failed lookup must raise")


async def _sync(fn):
    fn()


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn if inspect.iscoroutinefunction(fn) else (lambda f=fn: _sync(f)))

if FAILS:
    print("room-collab relay: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"room-collab relay: {sum(n.startswith('test_') for n in list(globals()))} passed")
