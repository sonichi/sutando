#!/usr/bin/env python3
"""The stage relay: a voice agent's local talk-highlight calls land on the room's page.

The failures worth pinning: a malformed topic reaching the stage, `speaking`
re-stamping the highlight (a deck would re-run it), and the port answering
before the page is connected as though a write had happened.

Run: python3 tests/room-collab-relay.test.py  (exit 0 pass / 1 fail)
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
    assert route("GET", "/highlight/x")[0] == 405
    assert route("POST", "/deck/other")[0] == 404
    assert route("POST", "/presenter/on")[0] == 200
    assert route("POST", "/slide/next") == ("slide", ("next", None))
    assert route("POST", "/slide/12") == ("slide", ("goto", 12))
    assert route("POST", "/slide/0")[0] == 400 and route("POST", "/slide/up")[0] == 400


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
    async def open_doc():
        yield page

    task = asyncio.create_task(serve(open_doc, 47811, log=lambda *_: None))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(47811, "POST", "/highlight/step4")
        assert status == 200 and body["topic"] == "step4", body
        ts = page.stage["ts"]
        status, _ = await http(47811, "POST", "/speaking/on")
        assert status == 200 and page.stage["speaking"] is True
        assert page.stage["ts"] == ts, "speaking must not re-stamp the highlight"
        status, body = await http(47811, "GET", "/state")
        assert body == {"topic": "step4", "ts": ts, "speaking": True}, body
        status, body = await http(47811, "POST", "/slide/next")
        first = body["seq"]
        status, body = await http(47811, "POST", "/slide/3")
        assert status == 200 and body["cmd"] == "goto" and body["n"] == 3 and body["seq"] > first, body
        status, body = await http(47811, "POST", "/highlight/%3Cx%3E")
        assert status == 400, body
    finally:
        task.cancel()


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab relay: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab relay: 2 passed")
