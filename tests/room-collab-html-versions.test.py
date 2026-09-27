#!/usr/bin/env python3
"""Named versions of an HTML page, written where the web client reads them.

Each page keeps `versions` (metadata) and `version_texts` (the source) in its own
document, under the web client's caps: 2 MB per snapshot, 50 per page, automatic
snapshots pruned before any named one.

Run: python3 tests/room-collab-html-versions.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Map, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-collab html versions: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from html_versions import (VERSION_CAP, VERSION_MAX_BYTES, VersionRefused, find_version,  # noqa: E402
                           plan_snapshot, read_versions)
from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import RoomDocError, text_root  # noqa: E402

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


def page_doc(kind="html"):
    doc = Doc()
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), text_root(kind) or "markdown", kind=kind)


def entry(i, auto):
    return {"id": f"v{i:09d}", "name": f"V{i}", "created": i, "by": "@a:x", "size": 1, "auto": auto}


async def test_save_writes_both_maps_in_the_web_clients_shape():
    doc, page = page_doc("html-ab12cd34")
    await page.append("<h1>Draft</h1>")
    saved = await page.save_version("  First  cut ", "@agent:x")
    meta = dict(doc.get("versions", type=Map)[saved["id"]])
    assert meta == {"name": "First cut", "created": saved["created"], "by": "@agent:x",
                    "size": 14, "auto": False}, meta
    assert doc.get("version_texts", type=Map)[saved["id"]] == "<h1>Draft</h1>"
    assert [v["name"] for v in page.versions] == ["First cut"]


async def test_restore_replaces_the_whole_page_and_saves_the_current_one_first():
    doc, page = page_doc()
    await page.append("<p>one</p>")
    first = await page.save_version("One", "@a:x")
    await page.append("<p>two</p>")
    out = await page.restore_version("one", "@a:x")  # by name, any case
    assert str(doc.get("html", type=Text)) == "<p>one</p>"
    assert out["restored"] == first["id"] and out["saved_before"], out
    auto = next(v for v in page.versions if v["auto"])
    assert auto["name"].startswith("Before restore ")
    assert doc.get("version_texts", type=Map)[auto["id"]] == "<p>one</p><p>two</p>"
    again = await page.restore_version(first["id"], "@a:x")  # by id; nothing to save
    assert again["saved_before"] is None


async def test_a_page_over_2mb_is_refused_with_a_clear_message():
    _, page = page_doc()
    await page.append("x" * (VERSION_MAX_BYTES + 1))
    try:
        await page.save_version("Deck", "@a:x")
    except RoomDocError as e:
        assert "too large to version" in str(e), e
    else:
        raise AssertionError("a 2 MB+ page must not be versioned")


async def test_the_cap_prunes_automatic_snapshots_first_and_keeps_named_ones():
    full = [entry(i, i in (3, 7)) for i in range(VERSION_CAP)]
    _, prune = plan_snapshot(full, "x", "new", "@a:x", 100)
    assert prune == ["v000000003"], prune
    try:
        plan_snapshot([entry(i, False) for i in range(VERSION_CAP)], "x", "new", "@a:x", 100)
    except VersionRefused as e:
        assert "50 named versions" in str(e)
    else:
        raise AssertionError("50 named versions must refuse a 51st")


async def test_restore_at_a_full_cap_of_named_versions_still_restores():
    doc, page = page_doc()
    meta, texts = doc.get("versions", type=Map), doc.get("version_texts", type=Map)
    for i in range(VERSION_CAP):
        e = entry(i, False)
        meta[e.pop("id")] = e
        texts[f"v{i:09d}"] = f"<p>{i}</p>"
    await page.append("<p>now</p>")
    out = await page.restore_version("v000000004", "@a:x")
    assert str(doc.get("html", type=Text)) == "<p>4</p>"
    assert out["saved_before"] is None and "without saving" in out["note"], out


async def test_the_list_is_read_in_shape_only_and_versions_are_html_only():
    listed = read_versions({"aaaaaaaaaa": {"name": "A", "created": 1}, "BAD": {},
                            "bbbbbbbbbb": {"name": "", "created": 2, "auto": True}, "cccccccccc": 3})
    assert [(v["id"], v["name"], v["auto"]) for v in listed] == [
        ("bbbbbbbbbb", "Untitled version", True), ("aaaaaaaaaa", "A", False)]
    assert find_version(listed, "nope") is None
    _, board = page_doc("board")
    try:
        board.versions
    except RoomDocError as e:
        assert "HTML page" in str(e)
    else:
        raise AssertionError("the board has no versions")


async def test_the_cli_parses_the_three_commands():
    import room_collab
    p = room_collab.build_parser()
    a = p.parse_args(["--kind", "html", "version-save", "!r:x", "--name", "Before review"])
    assert a.command == "version-save" and a.version_name == "Before review" and a.name is None
    a = p.parse_args(["--kind", "html-ab12cd34", "version-restore", "!r:x", "Before review"])
    assert a.version == "Before review"
    assert p.parse_args(["versions", "!r:x"]).command == "versions"


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab html versions: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab html versions: ok")
