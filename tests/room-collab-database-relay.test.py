#!/usr/bin/env python3
"""The relay's /db routes: what the voice tools call to read and write a room's databases.

What is pinned: query-string values reach the model by property name, a refused
value answers 400 and writes nothing, the routes work whichever surface the relay
holds (the databases are opened for the one call otherwise), and the stage calls
refuse on the databases surface rather than failing obscurely.

Run: python3 tests/room-collab-database-relay.test.py  (exit 0 pass / 1 fail)
"""
# ruff: noqa: E402 — imports follow the sys.path insert below
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
    print(f"room-collab database relay: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc
from room_collab_protocol import HTML_KIND
from room_collab_relay import route, serve
from room_database import MAPS, create_plan

FAILS = []
ME = "@air.agent:x"


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


def surface(kind):
    d = Doc()
    page = RoomDoc(FakeWS(), d, Awareness(d), "html" if kind == HTML_KIND else "markdown", kind=kind)
    page.ended = asyncio.Event()

    async def closed():
        await page.ended.wait()
    page.closed = closed
    page.settle = lambda s=0: asyncio.sleep(0)
    return page


async def http(port, method, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_routes():
    assert route("GET", "/db") == ("db", {"op": "list", "db": None, "view": None})
    assert route("GET", "/db/Demo%20day") == ("db", {"op": "view", "db": "Demo day", "view": None})
    assert route("GET", "/db/d1/view/By%20status") == ("db", {"op": "view", "db": "d1", "view": "By status"})
    assert route("POST", "/db/d1/row?set=Name%3DMark&set=Status%3DConfirmed") == \
        ("db", {"op": "add", "db": "d1", "set": ["Name=Mark", "Status=Confirmed"]})
    assert route("POST", "/db/d1/row/Mark?set=Minutes%3D10")[1]["op"] == "update"
    assert route("POST", "/db/d1/row/Mark")[0] == 400, "an update with nothing to set"
    assert route("POST", "/db/d1/row?set=Name")[0] == 400, "a set without ="
    assert route("POST", "/db/d1/move?row=Mark&to=Done") == \
        ("db", {"op": "move", "db": "d1", "row": "Mark", "to": "Done", "view": None})
    assert route("POST", "/db/d1/move?row=Mark")[0] == 400
    assert route("GET", "/db/d1/row")[0] == 405 and route("POST", "/db")[0] == 405
    assert route("POST", "/surface/db") == ("surface", "db")
    assert route("GET", "/db/d1/row/Mark") == ("db", {"op": "row", "db": "d1", "row": "Mark"})
    assert route("POST", "/db/d1/row/Mark/body?text=%23%20Hi%26more&append=1") == \
        ("db", {"op": "body", "db": "d1", "row": "Mark", "text": "# Hi&more", "append": True})
    assert route("POST", "/db/d1/row/Mark/body?text=")[1]["text"] == "", "an empty body clears it"
    assert route("POST", "/db/d1/row/Mark/body")[0] == 400, "a body write needs text="
    assert route("GET", "/db/d1/row/Mark/body")[0] == 405
    assert route("POST", "/db/d1/row/Mark/body?text=" + "x" * 16_001)[0] == 400
    assert route("POST", "/db/d1/row/Mark/body?text=" + "x" * 16_000)[0] == "db", "bodies get a larger cap"


async def test_the_voice_calls_read_and_write_the_databases():
    page, dbs = surface(HTML_KIND), surface("db")
    maps = {m: {} for m in MAPS}
    db, w = create_plan(maps, "demo_day", ME)
    await dbs.put_database(w)
    opened = []

    @asynccontextmanager
    async def open_doc(room):
        yield page

    @asynccontextmanager
    async def open_kind(room, kind):
        opened.append(kind)
        yield dbs

    port = 47871
    task = asyncio.create_task(serve(open_doc, port, room="!a:x", log=lambda *_: None,
                                     open_kind=open_kind, identity=lambda: ME))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(port, "GET", "/db")
        assert status == 200 and body["databases"][0]["name"] == "Demo day", body
        assert opened == ["db"], "the page stays held; the databases open for the call"
        status, body = await http(port, "POST", "/db/demo%20day/row?set=Use%20case%3DShared%20browser"
                                                "&set=Presenter%3DMark&set=Killer%20use%20case%3DConfirmed")
        assert status == 200 and body["row"], body
        row = body["row"]
        assert dbs.database["cells"][f"{db}|{row}|status"] == {"v": "o2", "updated": dbs.database["cells"][
            f"{db}|{row}|status"]["updated"], "by": ME}
        before = dbs.database
        status, body = await http(port, "POST", "/db/demo%20day/row?set=Killer%20use%20case%3DMaybe")
        assert status == 400 and "Proposed, Tried by others, Confirmed" in body["error"], body
        assert dbs.database == before, "a refusal writes nothing"
        status, body = await http(port, "POST", "/db/-/row/shared%20browser?set=Minutes%3D10")
        assert status == 200 and dbs.database["cells"][f"{db}|{row}|minutes"]["v"] == 10, body
        status, body = await http(port, "POST", "/db/-/move?row=Shared%20browser&to=Tried%20by%20others")
        assert status == 200 and body["view"] == "By status", body
        status, body = await http(port, "GET", "/db/-/view/by%20status")
        got = {g["name"]: g["rows"] for g in body["groups"]}
        assert got["Tried by others"] == [row] and body["rows"][0]["values"]["Minutes"] == "10", body

        status, body = await http(port, "GET", "/db/-/row/shared%20browser")
        assert status == 200 and body["title"] == "Shared browser" and body["body"] == "", body
        assert body["values"]["Presenter"] == "Mark" and body["row"] == row, body
        status, body = await http(port, "POST", "/db/-/row/shared%20browser/body?text=%23%20Plan%0A%0A-%20d%C3%A9mo")
        assert status == 200 and body["set"] is True and dbs.row_body(db, row) == "# Plan\n\n- démo", body
        status, body = await http(port, "POST", f"/db/-/row/{row}/body?text=%0A-%20ship&append=1")
        assert status == 200 and body["appended"] is True and dbs.row_body(db, row) == "# Plan\n\n- démo\n- ship"
        status, body = await http(port, "GET", "/db/-/row/nope")
        assert status == 400 and "no row" in body["error"], body
        status, body = await http(port, "POST", "/db/-/row/nope/body?text=x")
        assert status == 400 and "no row" in body["error"], body
        big = "x" * 16_000
        status, body = await http(port, "POST", f"/db/-/row/{row}/body?text={big}")
        assert status == 200 and len(dbs.row_body(db, row)) == 16_000, "a long body fits the request line"
        status, body = await http(port, "POST", "/surface/db")
        assert status == 200 and body["surface"] == "db" and body["parts"] == 1, body
        n = len(opened)
        status, body = await http(port, "GET", "/db/Demo%20day")
        assert status == 200 and body["view"]["name"] == "By date" and len(opened) == n, "the held surface serves it"
        status, body = await http(port, "POST", "/slide/next")
        assert status == 409, body
        status, body = await http(port, "GET", "/state")
        assert body["surface"] == "db" and body["topic"] is None, body
        status, body = await http(port, "GET", "/outline")
        assert body["databases"][0]["id"] == db, body
    finally:
        task.cancel()


async def test_writes_need_an_identity():
    page, dbs = surface(HTML_KIND), surface("db")
    _, w = create_plan({m: {} for m in MAPS}, "tasks", ME)
    await dbs.put_database(w)

    @asynccontextmanager
    async def open_doc(room):
        yield page

    @asynccontextmanager
    async def open_kind(room, kind):
        yield dbs

    port = 47872
    task = asyncio.create_task(serve(open_doc, port, room="!a:x", log=lambda *_: None, open_kind=open_kind))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(port, "GET", "/db/tasks")
        assert status == 200, body
        status, body = await http(port, "POST", "/db/tasks/row?set=Name%3Dx")
        assert status == 503 and "identity" in body["error"], body
        status, body = await http(port, "POST", "/db/tasks/row/x/body?text=hi")
        assert status == 503 and "identity" in body["error"], "a body write is signed too"
    finally:
        task.cancel()


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab database relay: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("room-collab database relay: ok")
