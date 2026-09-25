#!/usr/bin/env python3
"""Search across a room's surfaces: one query over Doc pages, HTML pages, database rows and sheet cells.

Worth pinning: every word must match, a title outranks body text, an HTML page
matches only on what a viewer sees, and each hit says how to open it.

Run: python3 tests/room-collab-search.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

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


def test_sheet_cells_are_named_by_address():
    rows = {"r0": {"order": 1}, "r1": {"order": 2}}
    cols = {"c0": {"order": 1}, "c1": {"order": 2}}
    cells = {"r1|c1": {"v": "Room-collab demo"}, "r0|c0": {"v": "Title"}, "zz|c0": {"v": "demo orphan"}}
    hits = search(sheet_records(rows, cols, cells), "demo")
    assert [h["cell"] for h in hits] == ["B2"], hits


def test_results_are_capped_and_snippets_are_short():
    recs = [doc_record(f"{i:08d}", f"Page {i}", "match " + "word " * 200) for i in range(80)]
    assert len(search(recs, "match", limit=500)) == LIMIT_MAX
    assert len(search(recs, "match", limit=3)) == 3
    s = snippet("x " * 300 + "needle " + "y " * 300, ["needle"], "needle")
    assert "needle" in s and s.startswith("…") and s.endswith("…") and len(s) < 200, s


def test_the_relay_and_the_spotlight_share_one_reading_of_a_page():
    from room_collab_relay import visible_words
    assert visible_words("<h1>Hi</h1><script>x</script><p>The<i>re</i></p>") == "hi there"


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab search: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab search: ok")
