"""The room's sheet, as an agent reads and writes it — the web client's model, in Python.

Rows and columns are maps of id → {order}; a cell is keyed `<rowId>|<colId>` and
holds {v, updated, by}. Addresses like "B4" are only a view: they are resolved to
ids at write time, so an edit lands in the cell the address names now.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import time

SHEET_KIND = "sheet"
ROWS_KEY, COLS_KEY, CELLS_KEY = "rows", "cols", "cells"
GAP = 1024
REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,6})$")


def ordered(axis: dict) -> list[str]:
    """Ids in display order; ties settle by id, as in the web client."""
    rows = [(v["order"], k) for k, v in axis.items()
            if isinstance(v, dict) and isinstance(v.get("order"), (int, float))]
    return [k for _, k in sorted(rows)]


def col_name(col: int) -> str:
    n, s = col + 1, ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_ref(ref: str) -> tuple[int, int]:
    """"B12" → (col 1, row 11); ValueError on anything else."""
    m = REF_RE.match(ref.strip())
    if not m:
        raise ValueError(f"not a cell address: {ref!r} (like A1 or B12)")
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + ord(ch) - 64
    return col - 1, int(m.group(2)) - 1


def starter_axes(rows: int = 50, cols: int = 12) -> tuple[dict, dict]:
    """The blank grid a room starts with: the same ids the web client makes."""
    return ({f"r{i:04d}": {"order": (i + 1) * GAP} for i in range(rows)},
            {f"c{i:03d}": {"order": (i + 1) * GAP} for i in range(cols)})


def grid(rows: dict, cols: dict, cells: dict) -> list[list[str]]:
    """The raw inputs (formulas as typed), row by row, trimmed to what holds something."""
    rs, cs = ordered(rows), ordered(cols)
    out = [[(cells.get(f"{r}|{c}") or {}).get("v", "") for c in cs] for r in rs]
    while out and not any(out[-1]):
        out.pop()
    width = max((i + 1 for row in out for i, v in enumerate(row) if v), default=0)
    return [row[:width] for row in out]


def _grow(axis: dict, need: int, prefix: str) -> dict:
    """New axis entries appended after the last, until there are `need`."""
    added: dict = {}
    last = max((v["order"] for v in axis.values() if isinstance(v, dict)), default=0)
    for i in range(need - len(ordered(axis))):
        added[f"{prefix}{int(time.time() * 1000):x}{secrets.token_hex(3)}{i}"] = {"order": last + (i + 1) * GAP}
    return added


def plan_writes(rows: dict, cols: dict, at: str, block: list[list[str]], by: str, now_ms: int | None = None):
    """Everything to write so `block` lands with its top-left at `at`: new rows and
    columns if the grid is too small, then one entry per cell (None removes it)."""
    col0, row0 = parse_ref(at)
    width = max((len(r) for r in block), default=0)
    new_rows = _grow(rows, row0 + len(block), "r")
    new_cols = _grow(cols, col0 + width, "c")
    rs = ordered({**rows, **new_rows})
    cs = ordered({**cols, **new_cols})
    now = now_ms or int(time.time() * 1000)
    writes = {}
    for i, line in enumerate(block):
        for j, v in enumerate(line):
            key = f"{rs[row0 + i]}|{cs[col0 + j]}"
            writes[key] = {"v": v[:50_000], "updated": now, "by": by} if v != "" else None
    return new_rows, new_cols, writes


def read_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text))]


def to_csv(block: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(block)
    return buf.getvalue()
