#!/usr/bin/env python3
"""The database's JavaScript semantics on the agent side: the web client filters, sorts and
checks values with JS rules (Number(), String(), ===), so the agent must reach the same answers.

Worth pinning: "12" and 12 compare as the web client compares them, a value that does not fit
its property is UNFIT (never written), and filters and board groups match the web client's.

Run: python3 tests/room-collab-database-semantics.test.py  (exit 0 pass / 1 fail)
"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from room_database import UNFIT, groups, js_number, js_str, normalize, query

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def test_numbers_and_strings_follow_javascript():
    assert js_number(None) == 0.0 and js_number(True) == 1.0 and js_number(False) == 0.0
    assert js_number(3) == 3.0 and js_number("  ") == 0.0 and js_number(" 12.5 ") == 12.5
    assert js_number("0x1f") == 31.0 and js_number("-Infinity") == -math.inf
    assert math.isnan(js_number("12px")) and math.isnan(js_number([1]))
    assert js_str(None) == "null" and js_str(True) == "true" and js_str(False) == "false"
    assert js_str(12.0) == "12" and js_str(1.5) == "1.5" and js_str("x") == "x"
    assert repr(UNFIT) == "UNFIT"


def test_a_value_that_does_not_fit_its_property_is_unfit():
    assert normalize({"type": "email"}, "a@b.co") == "a@b.co"
    assert normalize({"type": "email"}, "nope") is UNFIT
    assert normalize({"type": "number"}, 12) == 12 and normalize({"type": "number"}, "1e3") == 1000
    assert normalize({"type": "number"}, 0.5) == 0.5
    assert normalize({"type": "number"}, "twelve") is UNFIT and normalize({"type": "number"}, [1]) is UNFIT
    assert normalize({"type": "checkbox"}, True) is True and normalize({"type": "checkbox"}, "false") is False
    assert normalize({"type": "checkbox"}, "yes") is UNFIT
    assert normalize({"type": "relation"}, ["r1", "r1", "r2"]) == ["r1", "r2"]
    assert normalize({"type": "relation"}, "x" * 90) is UNFIT
    files = [{"name": "a.png", "url": "mxc://h/a"}]
    assert normalize({"type": "files"}, files) == files and normalize({"type": "files"}, [{"name": 1}]) is UNFIT
    assert normalize({"type": "text"}, "") is None and normalize({"type": "text"}, []) is None


P = [
    {"id": "name", "name": "Name", "type": "title", "order": 1},
    {"id": "n", "name": "Minutes", "type": "number", "order": 2},
    {"id": "done", "name": "Done", "type": "checkbox", "order": 3},
    {"id": "tags", "name": "Tags", "type": "multi_select", "order": 4,
     "options": [{"id": "t1", "name": "Demo"}, {"id": "t2", "name": "Docs"}]},
    {"id": "who", "name": "Who", "type": "person", "order": 5},
]


def db():
    rows, cells = [], {}
    for i, (name, n, done, tags, who) in enumerate([
            ("Alpha", 10, True, ["t1"], ["@a:x"]),
            ("Beta", 3, False, ["t2"], ["@b:x"]),
            ("Gamma", None, None, [], [])]):
        rid = f"r{i}"
        rows.append({"id": rid, "order": i + 1, "created": 1, "by": "@a:x"})
        for pid, v in (("name", name), ("n", n), ("done", done), ("tags", tags), ("who", who)):
            if v not in (None, []):
                cells[f"{rid}|{pid}"] = {"v": v}
    return {"id": "d", "props": P, "rows": rows, "views": [], "cells": cells}


def names(rows):
    return [r["id"] for r in rows]


def test_filters_match_the_web_client():
    d = db()

    def where(*f):
        return names(query(d, {"filter": list(f)}))
    assert where({"prop": "n", "op": "empty"}) == ["r2"]
    assert where({"prop": "n", "op": "not_empty"}) == ["r0", "r1"]
    assert where({"prop": "done", "op": "checked"}) == ["r0"]
    assert where({"prop": "done", "op": "unchecked"}) == ["r1", "r2"]
    assert where({"prop": "name", "op": "contains", "value": "ET"}) == ["r1"]
    assert where({"prop": "tags", "op": "is", "value": "t2"}) == ["r1"]
    assert where({"prop": "tags", "op": "is_not", "value": "t2"}) == ["r0", "r2"]
    assert where({"prop": "name", "op": "is", "value": "gamma"}) == ["r2"], "a title matches case-blind"
    assert where({"prop": "n", "op": "gt", "value": "5"}) == ["r0"], '"5" compares as a number'
    assert where({"prop": "n", "op": "lt", "value": 5}) == ["r1", "r2"], "an empty value compares as text, as in JS"
    assert where({"prop": "name", "op": "gt", "value": "b"}) == ["r1", "r2"], "text compares as text"
    assert where({"prop": "name", "op": "lt"}) == ["r0", "r1", "r2"], "no value compares with 'undefined'"
    assert where({"prop": "gone", "op": "is", "value": 1}) == ["r0", "r1", "r2"], "an unknown property filters nothing"
    assert where({"prop": "n", "op": "someday"}) == ["r0", "r1", "r2"]


def test_sorting_puts_empties_last_and_numbers_in_number_order():
    d = db()
    up = names(query(d, {"sort": [{"prop": "n", "dir": "asc"}]}))
    down = names(query(d, {"sort": [{"prop": "n", "dir": "desc"}]}))
    assert up == ["r1", "r0", "r2"] and down == ["r0", "r1", "r2"], (up, down)
    assert names(query(d, {"sort": [{"prop": "gone"}]})) == ["r0", "r1", "r2"]


def test_board_groups_by_option_by_person_or_not_at_all():
    d = db()
    rows = query(d, {})
    assert groups(d, {"groupBy": None}, rows) == [{"id": None, "name": "All", "rows": rows}]
    people = groups(d, {"groupBy": "who"}, rows)
    assert [(g["id"], g["name"], names(g["rows"])) for g in people] == [
        ("@a:x", "@a:x", ["r0"]), ("@b:x", "@b:x", ["r1"]), (None, "No Who", ["r2"])], people


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab database semantics: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab database semantics: ok")
