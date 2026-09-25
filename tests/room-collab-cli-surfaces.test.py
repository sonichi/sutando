#!/usr/bin/env python3
"""The CLI on every surface an agent can work on: HTML page, its pages and versions, sheet,
databases, the Doc's pages, and search — each command run through `room_collab.main`.

Worth pinning: a command on the wrong surface is refused before anything is written, and each
command prints what an agent needs next (a page's kind, a version's id, the cells written).

Run: python3 tests/room-collab-cli-surfaces.test.py  (exit 0 pass / 1 fail)
"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import asyncio
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab cli surfaces: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

import room_collab
import room_collab_client
from room_collab_client import RoomDoc
from room_collab_protocol import text_root

FAILS = []
BY = "@sutando-x:ag2.space"
BASE = ["--url", "https://h", "--token", "t", "--user-id", BY, "--settle", "0"]
PAGE = ('<section class="slide" data-id="intro"><h1>Launch plan</h1>'
        '<p data-topic="why">Why we ship Friday</p></section>')


class FakeWS:
    def __init__(self):
        self.sent = 0

    async def send(self, payload):
        self.sent += 1


class Room:
    """One room's documents, one per kind, kept across CLI calls as the server keeps them."""

    def __init__(self):
        self.docs = {}
        self.opened = []

    def doc(self, kind):
        if kind not in self.docs:
            d = Doc()
            self.docs[kind] = RoomDoc(FakeWS(), d, Awareness(d), text_root(kind) or "markdown", kind=kind)
        return self.docs[kind]


def cli(argv, room, kind=None):
    # RoomDoc binds a future to the current loop; each CLI call then runs in its own.
    asyncio.set_event_loop(asyncio.new_event_loop())

    @contextlib.asynccontextmanager
    async def fake_open(url, room_id, token, *, kind="markdown", insecure=False):
        room.opened.append(kind)
        yield room.doc(kind)

    real = room_collab_client.open_room_collab
    room_collab_client.open_room_collab = fake_open
    room_collab.open_room_collab = fake_open
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = room_collab.main(BASE + (["--kind", kind] if kind else []) + argv)
        return rc, out.getvalue(), err.getvalue()
    finally:
        room_collab_client.open_room_collab = real
        room_collab.open_room_collab = real


def ok(argv, room, kind=None):
    rc, out, err = cli(argv, room, kind)
    assert rc == 0, (argv, err)
    return out


def refused(argv, room, kind, words):
    rc, _, err = cli(argv, room, kind)
    assert rc == 2 and words in err, (argv, rc, err)


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except SystemExit as e:
        FAILS.append(f"{name}: the CLI exited {e.code} instead of running")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def test_the_html_page_state_slide_highlight_and_anchors():
    room = Room()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(room.doc("html").append(PAGE))
    assert json.loads(ok(["state", "!r:x"], room, "html")) == {}
    assert json.loads(ok(["state", "!r:x", "votes", '{"a": 1}'], room, "html")) == {
        "ok": True, "key": "votes", "deleted": False}
    assert json.loads(ok(["state", "!r:x", "votes"], room, "html")) == {"a": 1}
    refused(["state", "!r:x", "votes", "not json"], room, "html", "must be JSON")
    assert json.loads(ok(["state", "!r:x", "votes", "null"], room, "html"))["deleted"] is True
    refused(["state", "!r:x"], room, "markdown", "for the HTML page")
    assert json.loads(ok(["slide", "!r:x", "2"], room, "html"))["n"] == 2
    assert json.loads(ok(["slide", "!r:x", "next"], room, "html"))["cmd"] == "next"
    refused(["slide", "!r:x", "next"], room, "sheet", "not a sheet command")
    assert json.loads(ok(["highlight", "!r:x", "why"], room, "html"))["topic"] == "why"
    assert room.doc("html").stage["topic"] == "why"
    ok(["highlight", "!r:x", "clear"], room, "html")
    refused(["highlight", "!r:x", "why"], room, "markdown", "for the HTML page")
    text = ok(["anchors", "!r:x"], room, "html")
    assert "el:data-id=intro" in text and "el:data-topic=why" in text, text
    assert json.loads(ok(["--json", "anchors", "!r:x"], room, "html"))[0]["anchor"] == "el:data-id=intro"
    refused(["anchors", "!r:x"], room, "markdown", "for an HTML page")


def test_an_empty_page_has_no_anchors():
    assert "no data-id" in ok(["anchors", "!r:x"], Room(), "html")


def test_versions_are_saved_listed_and_restored():
    room = Room()
    assert "no versions yet" in ok(["versions", "!r:x"], room, "html")
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(room.doc("html").append("<p>first</p>"))
    saved = json.loads(ok(["version-save", "!r:x", "--name", "First"], room, "html"))
    assert saved["ok"] and saved["name"] == "First", saved
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(room.doc("html").append("<p>second</p>"))
    listed = ok(["versions", "!r:x"], room, "html")
    assert "First" in listed and saved["id"] in listed, listed
    assert json.loads(ok(["--json", "versions", "!r:x"], room, "html"))[0]["id"] == saved["id"]
    ok(["version-restore", "!r:x", "first"], room, "html")
    assert room.doc("html").text == "<p>first</p>"
    assert "(automatic)" in ok(["versions", "!r:x"], room, "html"), "restore kept a backup"
    refused(["versions", "!r:x"], room, "markdown", "for an HTML page")


def test_html_and_doc_pages_are_listed_and_added():
    room = Room()
    out = ok(["page-add", "!r:x", "Appendix"], room)
    assert "write it with --kind html-" in out, out
    added = json.loads(ok(["--json", "page-add", "!r:x", "Notes", "--kind", "markdown"], room))
    assert added["kind"].startswith("markdown-") and room.opened[-1] == "markdown", (added, room.opened)
    listed = ok(["pages", "!r:x", "--kind", "markdown"], room)
    assert "Main" in listed and "Notes" in listed and "Appendix" not in listed, listed
    as_json = json.loads(ok(["--json", "pages", "!r:x"], room))
    assert [p["title"] for p in as_json] == ["Main", "Appendix"], as_json
    rc, _, err = cli(["page-add", "!r:x", "   "], room)
    assert rc == 2 and "needs a title" in err, err


def test_the_sheet_starts_blank_then_reads_sets_and_imports():
    room = Room()
    first = ok(["read", "!r:x"], room, "sheet")
    assert first == "(the sheet is empty)", repr(first)
    rows, cols, _ = room.doc("sheet").sheet
    assert len(rows) == 50 and len(cols) == 12, "a room that never had a sheet gets the web client's grid"
    assert json.loads(ok(["set", "!r:x", "B2", "=1+1"], room, "sheet"))["cells"] == 1
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
        fh.write("name,when\nBassil,Sep 25\n")
    out = json.loads(ok(["import", "!r:x", fh.name, "--at", "A4"], room, "sheet"))
    assert out["cells"] == 4 and out["rows_added"] == 0, out
    got = ok(["read", "!r:x"], room, "sheet").splitlines()
    assert got == [",", ",=1+1", ",", "name,when", "Bassil,Sep 25"], got
    assert json.loads(ok(["--json", "read", "!r:x"], room, "sheet"))["A5"] == "Bassil"
    refused(["set", "!r:x", "not-a-cell", "x"], room, "sheet", "not a cell address")
    assert json.loads(ok(["--json", "peers", "!r:x"], room, "sheet")) == []


def test_templates_list_and_start_a_page_without_overwriting_one():
    lib = Path(tempfile.mkdtemp())
    (lib / "index.json").write_text(json.dumps({
        "templates": [{"id": "poll", "name": "Live poll", "description": "vote", "file": "poll.html"}],
        "scope": {"supported": ["scripts"], "not_supported": ["forms"]}}))
    (lib / "poll.html").write_text("<h1>Poll</h1>")
    room, url = Room(), lib.as_uri() + "/"
    listed = ok(["templates", "!r:x", "--library", url], room, "html")
    assert "poll: Live poll — vote" in listed and "not supported: forms" in listed, listed
    assert json.loads(ok(["--json", "templates", "!r:x", "--library", url], room, "html"))["templates"]
    assert json.loads(ok(["templates", "!r:x", "--library", url, "--use", "poll"], room, "html"))["chars"] == 13
    assert room.doc("html").text == "<h1>Poll</h1>"
    refused(["templates", "!r:x", "--library", url, "--use", "poll"], room, "html", "--replace")
    ok(["templates", "!r:x", "--library", url, "--use", "poll", "--replace"], room, "html")
    assert room.doc("html").text == "<h1>Poll</h1>"
    refused(["templates", "!r:x", "--library", url, "--use", "nope"], room, "html", "no template")
    refused(["templates", "!r:x", "--library", url], room, "markdown", "for the HTML page")
    refused(["templates", "!r:x", "--library", (lib / "missing").as_uri() + "/"], room, "html",
            "could not read the template library")


def test_the_room_is_searched_from_the_cli():
    room = Room()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(room.doc("markdown").append("# Launch\nFriday"))
    out = ok(["search", "!r:x", "launch"], room)
    assert out.startswith("Doc") and "Main" in out, out
    body = json.loads(ok(["--json", "search", "!r:x", "friday"], room))
    assert body["hits"][0]["go"] == ["/surface/doc"] and body["failed"] == [], body
    assert "no matches" in ok(["search", "!r:x", "absent"], room)


def test_the_databases_are_listed():
    room = Room()
    assert "no databases in this room" in ok(["dbs", "!r:x"], room, "db")
    ok(["create", "!r:x", "--template", "tasks"], room, "db")
    assert json.loads(ok(["--json", "dbs", "!r:x"], room, "db"))[0]["name"] == "Tasks"
    assert json.loads(ok(["--json", "peers", "!r:x"], room, "db")) == []


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab cli surfaces: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab cli surfaces: ok")
