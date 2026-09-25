#!/usr/bin/env python3
"""Search across a room's surfaces: one query over Doc pages, HTML pages, database rows and sheet cells.

Worth pinning: every word must match, a title outranks body text, an HTML page
matches only on what a viewer sees, and each hit says how to open it.

Run: python3 tests/room-collab-search.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from room_collab_protocol import RoomDocError  # noqa: E402
from room_database import add_row_plan, create_plan, read_db  # noqa: E402
from room_search import (LIMIT_MAX, db_records, doc_record, html_record, search,  # noqa: E402
                         sheet_records, snippet, visible_text)

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def merge(maps, writes):
    for m, entries in writes.items():
        maps.setdefault(m, {}).update(entries)


def test_every_word_must_match_and_case_is_ignored():
    recs = [doc_record(None, "Main", "The demo day is Friday."), doc_record("ab12cd34", "Notes", "Demo only.")]
    assert [h["title"] for h in search(recs, "DEMO friday")] == ["Main"]
    assert search(recs, "demo nothing") == []
    assert search(recs, "   ") == []


def test_a_title_or_heading_outranks_body_text():
    recs = [doc_record(None, "Main", "we talked about the budget at length"),
            doc_record("ab12cd34", "Budget", "numbers"),
            doc_record("cd34ef56", "Plans", "# Budget review\nlater")]
    assert [h["title"] for h in search(recs, "budget")] == ["Budget", "Plans", "Main"]


def test_html_matches_only_what_a_viewer_sees():
    src = ("<style>.secret{}</style><script>const secret = 1</script><!-- secret -->"
           "<h1>Live <b>poll</b></h1><p>Which demo goes first?</p>")
    rec = html_record("ab12cd34", "Live poll", src)
    assert search([rec], "secret") == []
    assert visible_text("<p>S<b>u</b>tando</p><p>next</p>") == "Sutando next"
    hit = search([rec], "demo first")[0]
    assert hit["go"] == ["/surface/html", "/page/ab12cd34"] and hit["kind"] == "html-ab12cd34", hit
    assert "Which demo goes first?" in hit["snippet"]


def test_database_rows_match_on_title_values_and_page_body():
    maps = {}
    db, w = create_plan(maps, "tasks", "@a:x", now_ms=1)
    merge(maps, w)
    row, w = add_row_plan(read_db(maps, db), "@a:x", {"name": "Write the demo", "priority": "High"}, now_ms=2)
    merge(maps, w)
    other, w = add_row_plan(read_db(maps, db), "@a:x", {"name": "Book the room"}, now_ms=3)
    merge(maps, w)
    recs = db_records(maps, {f"{db}|{other}": "Ask facilities about the projector."})
    hit = search(recs, "high")[0]
    assert hit["row"] == row and hit["title"] == "Write the demo" and hit["db_name"] == "Tasks", hit
    assert hit["go"] == ["/surface/db", f"/db/{db}/row/{row}"]
    assert search(recs, "projector")[0]["row"] == other
    assert search(recs, "@a:x") == [], "who made a row is not its content"


def test_a_sheet_row_matches_across_its_cells():
    rows = {"r0": {"order": 1}, "r1": {"order": 2}, "r2": {"order": 3}}
    cols = {"c0": {"order": 1}, "c1": {"order": 2}}
    cells = {"r1|c0": {"v": "Bassil"}, "r1|c1": {"v": "Room-collab demo"}, "r0|c0": {"v": "Presenter"},
             "zz|c0": {"v": "demo orphan"}}
    hits = search(sheet_records(rows, cols, cells), "bassil demo")
    assert [(h["sheet_row"], h["title"]) for h in hits] == [(2, "Bassil")], hits
    assert "Room-collab demo" in hits[0]["snippet"]


def test_results_are_capped_and_snippets_are_short():
    recs = [doc_record(f"{i:08d}", f"Page {i}", "match " + "word " * 200) for i in range(80)]
    assert len(search(recs, "match", limit=500)) == LIMIT_MAX
    assert len(search(recs, "match", limit=3)) == 3
    s = snippet("x " * 300 + "needle " + "y " * 300, ["needle"], "needle")
    assert "needle" in s and s.startswith("…") and s.endswith("…") and len(s) < 200, s


def test_the_relay_and_the_spotlight_share_one_reading_of_a_page():
    from room_collab_relay import visible_words
    assert visible_words("<h1>Hi</h1><script>x</script><p>The<i>re</i></p>") == "hi there"


class FakeWS:
    async def send(self, payload):
        pass


def opened(kind):
    from pycrdt import Awareness, Doc
    from room_collab_client import RoomDoc
    from room_collab_protocol import text_root
    d = Doc()
    s = RoomDoc(FakeWS(), d, Awareness(d), text_root(kind), kind=kind)
    s.closed = lambda: asyncio.sleep(3600)
    s.settle = lambda *_: asyncio.sleep(0)
    return s


async def a_room():
    """A Doc with one page, an HTML page, a database row with a page body, and a sheet that will not open."""
    from room_database import BODIES, key
    main, html = opened("markdown"), opened("html")
    await main.append("# Plan\nNothing yet.")
    page = await main.add_page("Launch notes", "@a:x")
    doc_page = opened(page["kind"])
    await doc_page.append("The launch is on Friday.")
    await html.append("<h1>Launch poll</h1><script>launch()</script>")
    db = opened("db")
    maps = {}
    dbid, w = create_plan(maps, "tasks", "@a:x", now_ms=1)
    merge(maps, w)
    row, w = add_row_plan(read_db(maps, dbid), "@a:x", {"name": "Ship it"}, now_ms=2)
    merge(maps, w)
    await db.put_database(maps)
    await db.put_row_body(dbid, row, "Launch checklist goes here.")
    docs = {"markdown": main, page["kind"]: doc_page, "html": html, "db": db}

    @asynccontextmanager
    async def open_kind(kind):
        if kind not in docs:
            raise RoomDocError(f"no {kind} here")
        yield docs[kind]

    return open_kind, page, dbid, row


def test_the_reader_searches_every_surface_and_reports_what_it_could_not_open():
    from room_collab_relay import search_room

    async def go():
        open_kind, page, dbid, row = await a_room()
        body = await search_room(open_kind, "launch", 10)
        got = {(h["surface"], h.get("title")): h["go"] for h in body["hits"]}
        assert got == {("doc", "Launch notes"): ["/surface/doc", f"/page/{page['id']}"],
                       ("html", "Main"): ["/surface/html"],
                       ("db", "Ship it"): ["/surface/db", f"/db/{dbid}/row/{row}"]}, got
        assert [f["kind"] for f in body["failed"]] == ["sheet"], body["failed"]
        assert body["searched"] == 4, body
    asyncio.run(go())


def test_the_relay_answers_search_and_refuses_an_empty_query():
    from room_collab_relay import route, serve
    assert route("GET", "/search?q=launch%20day&limit=3") == ("search", {"q": "launch day", "limit": 3})
    assert route("GET", "/search?q=%20")[0] == 400
    assert route("GET", "/search?q=x&limit=lots")[0] == 400

    async def go():
        open_kind, *_ = await a_room()
        html = opened("html")

        @asynccontextmanager
        async def open_doc(room):
            yield html
        port = 47863
        task = asyncio.create_task(serve(open_doc, port, room="!a:x", log=lambda *_: None,
                                         open_kind=lambda room, kind: open_kind(kind)))
        await asyncio.sleep(0.2)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /search?q=friday HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            raw = await r.read()
            w.close()
            head, _, body = raw.partition(b"\r\n\r\n")
            body = json.loads(body)
            assert int(head.split()[1]) == 200 and body["room"] == "!a:x", body
            assert [h["title"] for h in body["hits"]] == ["Launch notes"], body
        finally:
            task.cancel()
    asyncio.run(go())


def test_the_cli_parses_search():
    import room_collab
    a = room_collab.build_parser().parse_args(["search", "!r:x", "launch day", "--limit", "5"])
    assert (a.command, a.query, a.limit) == ("search", "launch day", 5)


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab search: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab search: ok")
