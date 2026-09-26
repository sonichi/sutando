#!/usr/bin/env python3
"""The sheet, as an agent writes it: addresses resolve to stable ids at write time.

The failures worth pinning: a write landing in the wrong cell after rows moved,
a CSV block that does not fit silently truncated, and ids that differ from the
web client's starter grid (two blank grids made at once would then double up).

Run: python3 tests/room-collab-sheet.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from pycrdt import Awareness, Doc  # noqa: E402

from room_collab_client import RoomDoc  # noqa: E402
from room_sheet import (col_name, grid, ordered, parse_ref, plan_writes, read_csv,  # noqa: E402
                        starter_axes, to_csv)


class FakeWS:
    async def send(self, payload):
        pass


assert parse_ref("B12") == (1, 11) and parse_ref("$aa$3") == (26, 2) and col_name(27) == "AB"
try:
    parse_ref("SUM")
except ValueError:
    pass
else:
    raise AssertionError("SUM is not an address")

rows, cols = starter_axes(3, 2)
assert ordered(rows) == ["r0000", "r0001", "r0002"] and ordered(cols) == ["c000", "c001"]

# A row inserted above: "A2" now names a different row, and a write goes where A2 is now.
rows["rX"] = {"order": 1024 + 512}
_, _, writes = plan_writes(rows, cols, "A2", [["x"]], "@a:x", now_ms=1)
assert list(writes) == ["rX|c000"], writes

# A block larger than the grid grows it rather than being cut.
new_rows, new_cols, writes = plan_writes(rows, cols, "B4", [["1", "2", "3"], ["4", "", "=A1"]], "@a:x", now_ms=1)
assert len(new_rows) == 1 and len(new_cols) == 2, (new_rows, new_cols)
assert sum(v is None for v in writes.values()) == 1, "an empty field clears its cell"

assert read_csv(to_csv([["a", 'say "hi", ok'], ["1", ""]])) == [["a", 'say "hi", ok'], ["1", ""]]


async def through_the_client():
    doc = Doc()
    page = RoomDoc(FakeWS(), doc, Awareness(doc), "markdown", kind="sheet")
    r, c = starter_axes(2, 2)
    await page.put_sheet(r, c, {})
    rows, cols, cells = page.sheet
    nr, nc, w = plan_writes(rows, cols, "A1", [["Q3", "120"], ["Q4", "=B1*2"]], "@a:x", now_ms=1)
    await page.put_sheet(nr, nc, w)
    assert grid(*page.sheet) == [["Q3", "120"], ["Q4", "=B1*2"]], grid(*page.sheet)
    _, _, w = plan_writes(*page.sheet[:2], "B2", [[""]], "@a:x")
    await page.put_sheet({}, {}, w)
    assert grid(*page.sheet) == [["Q3", "120"], ["Q4", ""]], grid(*page.sheet)


asyncio.run(through_the_client())
print("room-collab sheet: ok")
