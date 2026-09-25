#!/usr/bin/env python3
"""The room database as an agent reads and writes it: the web client's model, mirrored.

The failures worth pinning: a value that does not fit its type being written, a
template whose property or option ids differ from the web client's (the two
sides would then read different columns), a view that sorts, filters or groups
differently, and a CLI write that lands partly before a refusal.

The cases are the web client's (cinny dbModel.smoke.ts). TEMPLATES are checked
against tests/fixtures/room-database-templates.parity.json, dumped from dbModel.ts;
set CINNY_DB_MODEL=<path to dbModel.ts> to also check the live TS file (needs node + yjs).

Run: python3 tests/room-collab-database.test.py  (exit 0 pass / 1 fail)
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from room_database import (MAPS, TEMPLATES, UNFIT, DbRefusal, add_row_plan, assignments,  # noqa: E402
                           create_plan, csv_records, display_value, group_target, groups,
                           import_plan, list_dbs, move_plan, normalize, query, read_db,
                           resolve_row, value_of, view_json)

FAILS = []
ME = "@q:x"


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def apply(maps, writes):
    for m, entries in writes.items():
        for k, v in entries.items():
            if v is None:
                maps[m].pop(k, None)
            else:
                maps[m][k] = v


def fresh(template, name=None):
    maps = {m: {} for m in MAPS}
    db, w = create_plan(maps, template, ME, name)
    apply(maps, w)
    return maps, db


def set_cell(maps, db, row, prop_id, v, by=ME, now=None):
    from room_database import cell_writes
    d = read_db(maps, db)
    apply(maps, {"cells": cell_writes(d, row, {prop_id: v}, by, now)})


def add(maps, db, values=None, now=None):
    row, w = add_row_plan(read_db(maps, db), ME, values or {}, now)
    apply(maps, w)
    return row


def refused(fn):
    try:
        fn()
    except DbRefusal as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def test_template_makes_typed_props_and_views():
    maps, db = fresh("tasks")
    d = read_db(maps, db)
    assert [x["name"] for x in list_dbs(maps)] == ["Tasks"]
    assert [p["type"] for p in d["props"]] == ["title", "status", "person", "date", "select"]
    assert [v["layout"] for v in d["views"]] == ["board", "table", "calendar"]
    assert d["views"][0]["groupBy"] == "status" and "id" not in maps["props"][f"{db}|status"]


def test_templates_match_the_web_client():
    fixture = json.loads((REPO / "tests" / "fixtures" / "room-database-templates.parity.json").read_text())
    assert TEMPLATES == fixture, "TEMPLATES drifted from dbModel.ts (regenerate the fixture from it)"
    live = os.environ.get("CINNY_DB_MODEL")
    if live and shutil.which("node"):
        code = f"import {{ TEMPLATES }} from {json.dumps(live)}; console.log(JSON.stringify(TEMPLATES));"
        out = subprocess.run(["node", "--experimental-transform-types", "--no-warnings",
                              "--input-type=module", "-e", code], capture_output=True, text=True,
                             cwd=str(Path(live).parents[5]), check=True).stdout
        assert json.loads(out) == TEMPLATES, "TEMPLATES differ from the live dbModel.ts"


def test_a_value_that_does_not_fit_is_refused():
    maps, db = fresh("tasks")
    title, status, person, due = read_db(maps, db)["props"][:4]
    r = add(maps, db)
    set_cell(maps, db, r, "name", "Write the demo")
    set_cell(maps, db, r, "status", "In progress")
    assert maps["cells"][f"{db}|{r}|status"]["v"] == "o1", "an option by name resolves to its id"
    assert normalize(status, "Nonsense") is UNFIT
    assert normalize(person, "mark") is UNFIT, "a person is an mxid"
    assert normalize(person, ["@mark:x", "@air.agent:x"]) == ["@mark:x", "@air.agent:x"], "agents can be assignees"
    assert normalize(due, "25/09/2026") is UNFIT
    assert normalize(due, "2026-09-25") == {"start": "2026-09-25"}
    assert normalize({"type": "number"}, "12.5") == 12.5
    assert normalize({"type": "url"}, "javascript:alert(1)") is UNFIT
    for empty in (None, "", []):
        assert normalize(title, empty) is None, "empty removes the cell"
    assert normalize({"type": "number"}, True) is UNFIT and normalize({"type": "number"}, "1_0") is UNFIT
    assert normalize({"type": "number"}, "0x10") == 16 and normalize({"type": "number"}, "Infinity") is UNFIT
    assert normalize({"type": "checkbox"}, "true") is True and normalize({"type": "checkbox"}, "yes") is UNFIT
    assert normalize({"type": "multi_select", "options": TEMPLATES["wiki"]["props"][1]["options"]},
                     ["Guide", "o0", "Decision"]) == ["o0", "o2"]
    assert normalize({"type": "created_time"}, "2026-01-01") is UNFIT, "automatic props are never written"
    msg = refused(lambda: set_cell(maps, db, r, "status", "Nonsense"))
    assert "Status" in msg and "Not started, In progress, Done" in msg, msg
    assert maps["cells"][f"{db}|{r}|status"]["v"] == "o1", "a refusal writes nothing"


def demo():
    maps, db = fresh("demo_day")

    def row(name, date, status):
        vals = {"name": name, "date": date, **({"status": status} if status else {})}
        return add(maps, db, vals)
    row("Cloud Sutando", "2026-10-02", "Proposed")
    row("Room-collab", "2026-09-25", "Confirmed")
    row("Shared browser", "2026-09-25", None)
    return maps, db


def test_views_filter_sort_group():
    maps, db = demo()
    d = read_db(maps, db)
    P = {p["id"]: p for p in d["props"]}
    by_date = query(d, d["views"][0])
    assert [display_value(value_of(d, r, P["name"]), P["name"]) for r in by_date] == \
        ["Room-collab", "Shared browser", "Cloud Sutando"]
    board = groups(d, d["views"][1], query(d, d["views"][1]))
    assert [(g["name"], len(g["rows"])) for g in board] == \
        [("Proposed", 1), ("Tried by others", 0), ("Confirmed", 1), ("No Killer use case", 1)]
    only = query(d, {**d["views"][0], "filter": [{"prop": "status", "op": "is", "value": "o2"}]})
    assert len(only) == 1
    by_name = query(d, {**d["views"][0], "filter": [{"prop": "status", "op": "is", "value": "Confirmed"}]})
    assert len(by_name) == 1, "`is` also matches the displayed name, case-blind"
    assert len(query(d, {**d["views"][0], "filter": [{"prop": "status", "op": "empty"}]})) == 1
    assert len(query(d, {**d["views"][0], "filter": [{"prop": "name", "op": "contains", "value": "SUTANDO"}]})) == 1
    desc = query(d, {**d["views"][0], "sort": [{"prop": "name", "dir": "desc"}]})
    assert [value_of(d, r, P["name"]) for r in desc] == ["Shared browser", "Room-collab", "Cloud Sutando"]


def test_numbers_sort_as_numbers_and_compare_with_gt():
    maps, db = fresh("demo_day")
    for n in (10, 9, 100):
        add(maps, db, {"minutes": n})
    d = read_db(maps, db)
    view = {**d["views"][0], "sort": [{"prop": "minutes", "dir": "asc"}]}
    assert [value_of(d, r, d["props"][3]) for r in query(d, view)] == [9, 10, 100]
    assert len(query(d, {**view, "filter": [{"prop": "minutes", "op": "gt", "value": "9"}]})) == 2
    assert display_value(10.0, d["props"][3]) == "10", "a number reads back as a float and shows as 10"


def test_a_board_move_writes_the_group_value():
    maps, db = fresh("tasks")
    r = add(maps, db)
    d = read_db(maps, db)
    status = d["props"][1]
    apply(maps, move_plan(d, r, status, group_target(d, status, "done"), ME))
    assert maps["cells"][f"{db}|{r}|status"]["v"] == "o2"
    apply(maps, move_plan(read_db(maps, db), r, status, group_target(d, status, "No Status"), ME))
    assert f"{db}|{r}|status" not in maps["cells"]
    assert "no column" in refused(lambda: group_target(d, status, "Blocked"))
    maps, db = fresh("wiki")
    d = read_db(maps, db)
    assert "several values" in refused(lambda: move_plan(d, "r", d["props"][1], "o0", ME))


def test_automatic_properties():
    maps, db = fresh("tasks")
    r = add(maps, db, now=1_790_000_000_000)
    set_cell(maps, db, r, "name", "a", by="@b:x", now=1_790_000_060_000)
    d = read_db(maps, db)
    row = d["rows"][0]
    assert value_of(d, row, {"id": "x", "type": "created_by"}) == [ME]
    assert value_of(d, row, {"id": "x", "type": "edited_by"}) == ["@b:x"]
    assert value_of(d, row, {"id": "x", "type": "created_time"}) == "2026-09-21T14:13"
    assert value_of(d, row, {"id": "x", "type": "edited_time"}) == "2026-09-21T14:14"


def test_names_as_typed_resolve_and_a_bad_one_refuses_everything():
    maps, db = fresh("tasks")
    d = read_db(maps, db)
    vals = assignments(d, ["name=Ship it", "STATUS=in progress", "Assignee=@a:x, @air.agent:x",
                           "Due=2026-09-25..2026-09-27", "Priority=high"])
    assert vals == {"name": "Ship it", "status": "o1", "assignee": ["@a:x", "@air.agent:x"],
                    "due": {"start": "2026-09-25", "end": "2026-09-27"}, "priority": "o0"}, vals
    msg = refused(lambda: assignments(d, ["Status=Blocked"]))
    assert "Not started, In progress, Done" in msg, msg
    assert "no property" in refused(lambda: assignments(d, ["Colour=red"]))
    msg = refused(lambda: add_row_plan(d, ME, {"name": "x", "due": "tomorrow"}))
    assert "Due" in msg and "YYYY-MM-DD" in msg, msg
    r = add(maps, db, {"name": "Ship it"})
    add(maps, db, {"name": "Twice"})
    add(maps, db, {"name": "twice"})
    d = read_db(maps, db)
    assert resolve_row(d, "ship IT")["id"] == r and resolve_row(d, r)["id"] == r
    assert "2 rows" in refused(lambda: resolve_row(d, "Twice"))


def test_a_csv_becomes_rows_with_headers_mapped_by_name():
    maps, db = fresh("demo_day")
    d = read_db(maps, db)
    text = ('note,"Note to presenters:\nkeep it short",,\n'
            "Team Demo Date,Persenter,Use case brief descipton ,Time needed (assuming 5~10m),Demo video,Extra\n"
            "Sep 25,John,Cloud Sutando,,https://v.example/1,x\n"
            ",Rui,tbd,,,\n"
            ",,,,,\n")
    recs, report = csv_records(d, text, header_row=2, year=2026,
                               mapping=["Team Demo Date=Demo date", "persenter=Presenter",
                                        "Use case brief descipton=Use case", "Time needed=Minutes"])
    assert recs == [{"date": "2026-09-25", "presenter": "John", "name": "Cloud Sutando",
                     "video": "https://v.example/1"},
                    {"presenter": "Rui", "name": "tbd"}], recs
    assert report["unmatched_headers"] == ["Extra"] and report["blank_rows_skipped"] == 1, report
    assert report["columns"]["Demo video"] == "Demo video", "a header equal to a property name maps itself"
    ids, w = import_plan(d, recs, ME)
    apply(maps, w)
    assert len(read_db(maps, db)["rows"]) == 2 and len(set(ids)) == 2
    assert "no year" in refused(lambda: csv_records(d, text, header_row=2, mapping=["Team=Demo date"]))
    assert "no CSV headers" in refused(lambda: csv_records(d, text, header_row=2, mapping=["Nope=Minutes"]))
    bad = "Minutes\n10\nten\n"
    msg = refused(lambda: import_plan(d, csv_records(d, bad)[0], ME))
    assert "record 2" in msg and "Minutes" in msg, msg


def test_view_json_shows_display_values_and_board_groups():
    maps, db = demo()
    d = read_db(maps, db)
    v = view_json(d, d["views"][1], "Demo day")
    assert v["view"]["layout"] == "board" and [g["name"] for g in v["groups"]][-1] == "No Killer use case"
    first = next(r for r in v["rows"] if r["values"]["Use case"] == "Room-collab")
    assert first["values"]["Killer use case"] == "Confirmed" and first["values"]["Demo date"] == "2026-09-25"


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab database: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("room-collab database: ok")
