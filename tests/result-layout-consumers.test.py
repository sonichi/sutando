#!/usr/bin/env python3
"""Consumers must see EVERY result layout, not the subset each once knew.

Three enumerators covered different subsets, so a result was findable or not
depending on which consumer asked. These cases fail at the parent commit:
each uses a layout the consumer under test could not previously reach.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("aapi_layouts", REPO / "src" / "agent-api.py")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)

tw_spec = importlib.util.spec_from_file_location("tw_layouts", REPO / "src" / "task_workstreams.py")
tw = importlib.util.module_from_spec(tw_spec)
sys.modules[tw_spec.name] = tw
tw_spec.loader.exec_module(tw)

fails = []


def check(name, got, want):
    if got != want:
        fails.append(name)
        print(f"  FAIL: {name}\n        got {got!r} want {want!r}")
    else:
        print(f"  OK: {name}")


def _rows_for(layout_rel: str, body: str = "archived body\n"):
    """Build a workspace whose only result sits at `layout_rel`, return the row."""
    original = (api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR = root / "tasks", root / "results", root
            api.TASK_DIR.mkdir()
            (api.RESULT_DIR / "archive").mkdir(parents=True)
            api.task_history.clear()
            task = api.TASK_DIR / "task-layout.txt"
            task.write_text("source: discord\ntask: layout probe\n")
            os.utime(task, (2000, 2000))
            dest = api.RESULT_DIR / layout_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body)
            rows = api._active_task_rows()
            return next((r for r in rows if r.get("id") == "task-layout"
                         or "layout probe" in str(r.get("text", ""))), None)
    finally:
        api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR = original


# _active_task_rows scanned ONLY `archive/*/`, unsorted: both layouts below
# were unreachable for it, and the month order was filesystem-dependent.
for layout in ("archive/task-layout.txt", "archive-legacy/task-layout.txt"):
    row = _rows_for(layout)
    check(f"_active_task_rows sees {layout}", (row or {}).get("status"), "done")

# month layout must still work (no regression)
row = _rows_for("archive/2026-08/task-layout.txt")
check("_active_task_rows still sees month layout", (row or {}).get("status"), "done")

# _exact_result previously missed the flat gateway form entirely.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "results" / "archive" / "task-flat-1787760000.txt").write_text("flat body\n")
    check("_exact_result sees flat gateway form",
          tw._exact_result(ws, "task-flat"), "flat body")

# An authoritative `pending` must outrank the in-memory cache: a torn archive
# candidate means the newest answer is mid-write, so the cache is superseded.
original = (api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR)
try:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR = root / "tasks", root / "results", root
        api.TASK_DIR.mkdir()
        (api.RESULT_DIR / "archive" / "2026-08").mkdir(parents=True)
        api.task_history.clear()
        task = api.TASK_DIR / "task-torn.txt"
        task.write_text("source: discord\ntask: torn probe\n")
        os.utime(task, (2000, 2000))
        api.task_history["task-torn"] = {"status": "done", "result": "OLD ANSWER - superseded",
                                         "text": "torn probe", "time": 1000, "source": "discord"}
        # Truncated multi-byte sequence: present, decodes fatally, not ready.
        (api.RESULT_DIR / "archive" / "2026-08" / "task-torn.txt").write_bytes(b"NEW ANSWER \xe2\x9c")
        row = next((r for r in api._active_task_rows() if "torn probe" in str(r.get("text", ""))), None)
        check("torn archive -> working, not the cached OLD ANSWER",
              ((row or {}).get("status"), (row or {}).get("result")), ("working", ""))
finally:
    api.TASK_DIR, api.RESULT_DIR, api.WORKSPACE_DIR = original

print()
print(f"{len(fails)} failure(s)" if fails else "all checks passed")
sys.exit(1 if fails else 0)
