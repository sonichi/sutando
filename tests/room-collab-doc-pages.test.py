#!/usr/bin/env python3
"""The Doc's pages: each is its own `markdown-<id>` document with the Doc's
roots, listed in the main Doc's `pages` map, as the HTML page's pages are.

The failures worth pinning: a Doc page not treated as the Doc where the Doc is
special, a page listed in the wrong index, a comment that does not say its
page, and the relay's /page/<id> opening an HTML page while the Doc is held.

Run: python3 tests/room-collab-doc-pages.test.py  (exit 0 pass / 1 fail)
"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import asyncio
import contextlib
import io
import json
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab doc pages: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

import room_collab
from room_collab_client import RoomDoc
from room_collab_protocol import (DEFAULT_KIND, HTML_KIND, RoomDocError, doc_page_kind,
                                  doc_socket_url, has_stage, is_html_kind, is_markdown_kind,
                                  main_kind, new_page_id, read_pages, text_root)
from room_collab_relay import route, serve, surface_kind

FAILS = []
PASSED = []
PAGE = "markdown-ab12cd34"


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


def opened(kind):
    d = Doc()
    s = RoomDoc(FakeWS(), d, Awareness(d), text_root(kind), kind=kind)
    s.closed = lambda: asyncio.sleep(3600)
    s.settle = lambda *_: asyncio.sleep(0)
    return s


async def test_a_doc_page_is_markdown_wherever_markdown_is_special():
    for k in (DEFAULT_KIND, PAGE, "markdown-00000000"):
        assert is_markdown_kind(k) and text_root(k) == "markdown" and has_stage(k), k
        assert main_kind(k) == DEFAULT_KIND and not is_html_kind(k), k
    for k in ("markdown-", "markdown-AB12CD34", "markdown-ab12cd345", "xmarkdown-ab12cd34",
              "html", "html-ab12cd34", "board", None, ""):
        assert not is_markdown_kind(k), k
    assert main_kind("html-ab12cd34") == HTML_KIND and main_kind("board") is None
    import re
    for _ in range(50):
        k = doc_page_kind(new_page_id())
        assert is_markdown_kind(k) and re.fullmatch(r"[a-z0-9-]{1,32}", k), k
    assert doc_page_kind(None) == DEFAULT_KIND
    assert doc_socket_url("https://x", "!r:x", kind=PAGE).endswith(f"kind={PAGE}")
    try:
        doc_page_kind("../x")
    except RoomDocError:
        pass
    else:
        raise AssertionError("a malformed page id must be refused")


async def test_a_doc_page_is_read_and_written_as_text():
    page = opened(PAGE)
    await page.append("# Notes\n\nfirst line")
    await page.replace("first", "second")
    assert page.text == "# Notes\n\nsecond line", page.text
    await page.navigate("next")
    assert page.stage["nav"]["cmd"] == "next"


async def test_the_doc_lists_its_own_pages_with_parent_and_icon():
    doc = opened(DEFAULT_KIND)
    a = await doc.add_page("Agenda", "@a:x")
    b = await doc.add_page("Details", "@a:x", parent=a["id"])
    assert a["kind"] == f"markdown-{a['id']}" and b["parent"] == a["id"], (a, b)
    assert [(p["title"], p["parent"]) for p in doc.pages] == [("Agenda", None), ("Details", a["id"])]
    try:
        await doc.add_page("Deeper", "@a:x", parent=b["id"])
    except RoomDocError as e:
        assert "one level" in str(e), e
    else:
        raise AssertionError("nesting is one level")
    try:
        opened(PAGE).pages
    except RoomDocError as e:
        assert "--kind markdown" in str(e), e
    else:
        raise AssertionError("a Doc page holds no page list")
    listed = read_pages({"aaaaaaa1": {"title": "A", "parent": "gone0000", "icon": " ⭐ "},
                         "bbbbbbb2": {"title": "B", "parent": "aaaaaaa1"}}, DEFAULT_KIND)
    assert [(p["kind"], p["parent"], p["icon"]) for p in listed] == [
        ("markdown-aaaaaaa1", None, "⭐"), ("markdown-bbbbbbb2", "aaaaaaa1", "")], listed


async def test_the_cli_lists_and_adds_doc_pages():
    p = room_collab.build_parser()
    a = p.parse_args(["pages", "--kind", "markdown", "!r:x"])
    assert room_collab.page_family(a) == DEFAULT_KIND
    a = p.parse_args(["pages", "--kind", PAGE, "!r:x"])
    assert room_collab.page_family(a) == DEFAULT_KIND, "a page's own kind finds its family"
    a = p.parse_args(["pages", "!r:x"])
    assert room_collab.page_family(a) == HTML_KIND, "the HTML page stays the default"
    a = p.parse_args(["page-add", "--kind", "markdown", "--parent", "ab12cd34", "!r:x", "Q3"])
    assert a.parent == "ab12cd34" and a.title == "Q3"
    try:
        room_collab.page_family(p.parse_args(["pages", "--kind", "board", "!r:x"]))
    except RoomDocError:
        pass
    else:
        raise AssertionError("the board has no pages")

    doc = opened(DEFAULT_KIND)
    args = types.SimpleNamespace(command="page-add", title="Agenda", user_id="@a:x", name=None,
                                 settle=0, json=True, parent=None, page_kind="markdown")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert await room_collab.pages(doc, args) == 0
    added = json.loads(out.getvalue())
    assert added["kind"] == f"markdown-{added['id']}", added
    args.command, out = "pages", io.StringIO()
    with contextlib.redirect_stdout(out):
        await room_collab.pages(doc, args)
    listed = json.loads(out.getvalue())
    assert listed[0] == {"id": None, "kind": "markdown", "title": "Main"}, listed
    assert listed[1]["title"] == "Agenda" and listed[1]["kind"] == added["kind"], listed


async def test_a_comment_on_a_doc_page_names_its_page():
    anchor = {"start": "s", "end": "e"}
    _, main = room_collab.comment_content(anchor, "q", 0, "hi")
    assert "page" not in main[room_collab.COMMENT_KEY], "the main Doc keeps the old shape"
    _, onpage = room_collab.comment_content(anchor, "q", 0, "hi", page="ab12cd34")
    assert onpage[room_collab.COMMENT_KEY]["page"] == "ab12cd34"
    body, extra = room_collab.summon_content("!r:x", "@b:x", PAGE, None, "Product  roadmap")
    marker = extra[room_collab.SUMMON_KEY]
    assert marker["kind"] == PAGE and marker["page_title"] == "Product roadmap", marker
    assert '"Product roadmap" in this room\'s Doc' in body, body
    body, extra = room_collab.summon_content("!r:x", "@b:x", PAGE)
    assert "page_title" not in extra[room_collab.SUMMON_KEY] and '"a page" in' in body, body
    body, extra = room_collab.summon_content("!r:x", "@b:x", "html-zz99zz99", None, "Poll")
    assert '"Poll" in this room\'s HTML page' in body and extra[room_collab.SUMMON_KEY]["kind"] == "html-zz99zz99"
    body, extra = room_collab.summon_content("!r:x", "@b:x", DEFAULT_KIND, None, "ignored")
    assert "page_title" not in extra[room_collab.SUMMON_KEY] and "ignored" not in body, "the main Doc names no page"


async def test_page_routes_follow_the_held_surface():
    assert route("POST", "/page/ab12cd34", "doc") == ("surface", PAGE)
    assert route("POST", "/page/ab12cd34", PAGE) == ("surface", PAGE)
    assert route("POST", "/page/main", PAGE) == ("surface", "doc")
    assert route("POST", "/page/ab12cd34", "html") == ("surface", "html-ab12cd34")
    assert route("POST", "/page/ab12cd34") == ("surface", "html-ab12cd34")
    assert route("POST", f"/surface/{PAGE}") == ("surface", PAGE)
    assert route("POST", "/surface/markdown-nope")[0] == 400
    assert surface_kind(PAGE) == PAGE and surface_kind("doc") == DEFAULT_KIND


async def http(port, method, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_the_relay_switches_to_a_doc_page_and_lists_the_docs_pages():
    html, main = opened(HTML_KIND), opened(DEFAULT_KIND)
    await main.append("# Main\n")
    entry = await main.add_page("Appendix", "@a:x")
    page = opened(entry["kind"])
    await page.append("# One\n\n## Two\n\nsome words here")
    opens = []

    @asynccontextmanager
    async def open_doc(room):
        opens.append("html")
        yield html

    @asynccontextmanager
    async def open_kind(room, kind):
        opens.append(kind)
        yield {DEFAULT_KIND: main, entry["kind"]: page, HTML_KIND: html}[kind]

    port = 47841
    task = asyncio.create_task(serve(open_doc, port, room="!a:x", log=lambda *_: None, open_kind=open_kind))
    await asyncio.sleep(0.2)
    try:
        status, body = await http(port, "POST", "/surface/doc")
        assert status == 200 and body["surface"] == "doc", body
        status, body = await http(port, "GET", "/pages")
        assert body["pages"] == [{"id": None, "title": "Main"}, {"id": entry["id"], "title": "Appendix"}], body
        status, body = await http(port, "POST", f"/page/{entry['id']}")
        assert status == 200 and body["surface"] == entry["kind"] and body["parts"] == 2, body
        status, body = await http(port, "GET", "/outline")
        assert [h["title"] for h in body["headings"]] == ["One", "Two"], body
        status, body = await http(port, "POST", "/spot/some%20words")
        assert body["found_on_page"] is True and page.stage["spot"]["text"] == "some words", body
        assert "spot" not in main.stage, "a spot on the page never reaches the main Doc"
        status, body = await http(port, "POST", "/page/zz99zz99")
        assert status == 404 and body["pages"] == [entry["id"]], body
        assert body["surface"] == entry["kind"], "an unlisted page leaves the held page in place"
        status, body = await http(port, "POST", "/page/main")
        assert body["surface"] == "doc", body
        assert opens[-1] == DEFAULT_KIND and entry["kind"] in opens, opens
    finally:
        task.cancel()


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab doc pages: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"room-collab doc pages: {len(PASSED)} passed")
