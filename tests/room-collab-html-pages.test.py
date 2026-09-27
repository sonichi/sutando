#!/usr/bin/env python3
"""A room's extra HTML pages: each is its own `html-<id>` document with the main
page's roots, listed in the main page's `pages` map.

Every HTML-only check goes through `is_html_kind`, so a page kind must be
accepted wherever `html` is, and nothing that merely resembles one may be.

Run: python3 tests/room-collab-html-pages.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "room-collab" / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    from pycrdt import Awareness, Doc, Map, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-collab html pages: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import (HTML_KIND, RoomDocError, doc_socket_url, has_stage,  # noqa: E402
                                  html_page_kind, is_html_kind, new_page_entry, new_page_id,
                                  read_pages, text_root)

FAILS = []
PAGE = "html-ab12cd34"


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


def page_doc(kind=PAGE):
    doc = Doc()
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), text_root(kind), kind=kind)


async def test_a_page_kind_is_html_and_nothing_else_is():
    for k in ("html", PAGE, "html-00000000"):
        assert is_html_kind(k) and text_root(k) == "html" and has_stage(k), k
    for k in ("html-", "html-AB12CD34", "html-ab12cd345", "xhtml-ab12cd34", "html-ab12cd3",
              "markdown", "board", None, ""):
        assert not is_html_kind(k), k
    assert text_root("markdown") == "markdown" and text_root("board") is None
    assert has_stage("board") and not has_stage("kanban")


async def test_a_page_id_makes_a_kind_the_server_accepts():
    import re
    for _ in range(100):
        k = html_page_kind(new_page_id())
        assert is_html_kind(k) and re.fullmatch(r"[a-z0-9-]{1,32}", k), k
    assert html_page_kind(None) == HTML_KIND
    assert doc_socket_url("https://x", "!r:x", kind=PAGE).endswith(f"kind={PAGE}")
    try:
        html_page_kind("../x")
    except RoomDocError:
        pass
    else:
        raise AssertionError("a malformed page id must be refused")


async def test_a_page_is_written_under_the_html_root_with_every_page_command():
    doc, page = page_doc()
    await page.append("<h1>Agenda</h1>")
    assert str(doc.get("html", type=Text)) == "<h1>Agenda</h1>"
    assert str(doc.get("markdown", type=Text)) == ""
    assert page.snapshot()["text"] == "<h1>Agenda</h1>"
    state = await page.set_stage("intro")
    assert page.stage["topic"] == "intro" and state["topic"] == "intro"
    await page.navigate("goto", 2)
    await page.set_spot("Agenda")
    await page.set_app_state("votes", {"a": 1})
    assert page.app_state == {"votes": {"a": 1}}


async def test_the_page_list_reads_in_order_and_skips_what_is_not_a_page():
    listed = read_pages({
        "zz000002": {"title": " Second  page ", "order": 2, "created": 5, "by": "@a:x"},
        "aa000001": {"title": "First", "order": 1, "created": 9},
        "bb000003": {"title": "", "order": 2, "created": 1},
        "NOT-ID": {"title": "bad"},
        "cc000004": "not an entry",
    })
    assert [(p["id"], p["title"], p["kind"]) for p in listed] == [
        ("aa000001", "First", "html-aa000001"),
        ("bb000003", "Untitled page", "html-bb000003"),
        ("zz000002", "Second page", "html-zz000002"),
    ], listed
    assert read_pages(None) == [] and read_pages([1]) == []
    entry = new_page_entry("x" * 200, "@q:x", listed, 42)
    assert entry == {"title": "x" * 80, "order": 3, "created": 42, "by": "@q:x"}, entry


async def test_add_page_lists_it_in_the_main_page_as_the_web_client_reads_it():
    doc, main = page_doc(HTML_KIND)
    added = await main.add_page("Q3 numbers", "@agent:x")
    assert is_html_kind(added["kind"]) and added["kind"] == f"html-{added['id']}"
    raw = doc.get("pages", type=Map)
    assert dict(raw[added["id"]]) == {"title": "Q3 numbers", "order": 1,
                                       "created": added["created"], "by": "@agent:x"}
    second = await main.add_page("Appendix", "@agent:x")
    assert [p["title"] for p in main.pages] == ["Q3 numbers", "Appendix"]
    assert second["order"] == 2


async def test_the_page_list_is_only_in_the_main_page():
    _, page = page_doc()
    try:
        page.pages
    except RoomDocError as e:
        assert "--kind html" in str(e), e
    else:
        raise AssertionError("an extra page holds no page list")


async def test_the_relay_serves_an_extra_page():
    from room_collab_relay import route, surface_kind
    assert route("POST", f"/surface/{PAGE}") == ("surface", PAGE)
    assert route("POST", "/page/ab12cd34") == ("surface", PAGE)
    assert route("POST", "/page/main") == ("surface", "html")
    assert route("GET", "/pages") == ("pages", None)
    assert route("POST", "/page/ab12")[0] == 400
    assert route("POST", "/surface/html-nope")[0] == 400
    assert route("GET", "/page/ab12cd34")[0] == 405
    assert surface_kind(PAGE) == PAGE and surface_kind("doc") == "markdown"
    assert surface_kind("html") == "html" and surface_kind("kanban") is None


async def test_the_cli_takes_a_page_kind_for_every_html_command():
    import room_collab
    p = room_collab.build_parser()
    a = p.parse_args(["--kind", PAGE, "highlight", "!r:x", "intro"])
    assert a.kind == PAGE
    a = p.parse_args(["pages", "!r:x"])
    assert a.command == "pages"
    a = p.parse_args(["page-add", "!r:x", "Q3 numbers"])
    assert a.command == "page-add" and a.title == "Q3 numbers"


async def test_the_cli_lists_and_adds_pages_through_the_main_page():
    import io
    import contextlib
    import types
    import room_collab
    doc, main = page_doc(HTML_KIND)
    args = types.SimpleNamespace(command="page-add", title="Agenda", user_id="@a:x", name=None,
                                 settle=0, json=True)
    main.settle = lambda *_: asyncio.sleep(0)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert await room_collab.pages(main, args) == 0
    assert '"kind": "html-' in out.getvalue(), out.getvalue()
    args.command, out = "pages", io.StringIO()
    with contextlib.redirect_stdout(out):
        await room_collab.pages(main, args)
    import json
    listed = json.loads(out.getvalue())
    assert listed[0] == {"id": None, "kind": "html", "title": "Main"}
    assert listed[1]["title"] == "Agenda" and listed[1]["kind"].startswith("html-")


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab html pages: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"room-collab html pages: {sum(1 for n in globals() if n.startswith('test_'))} passed")
