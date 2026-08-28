#!/usr/bin/env python3
"""`/tasks/history` must answer from the readiness owner, not its own index.

Durable history built a private mtime-keyed result index and decoded any body it
found with `errors="replace"`. Two consequences, both measured on a live head:

  * a torn body rendered as a DONE answer ending in U+FFFD, while `/result`
    correctly said pending -- the owner could act on a truncated answer
  * an archived result carrying the epoch suffix (`task-<id>-<epoch>.txt`) was
    keyed by that stem rather than the owning task id, so a valid answer was
    reported as done-and-empty and permanently missed

Pinned at `task_history_payload()` -- the path the web client hydrates -- and
not at `_exact_result()`, which was already delegated and was never the
/tasks/history path.

Run: python3 tests/task-history-readiness.test.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402
import task_workstreams as tw  # noqa: E402

results: list[bool] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"   got={got!r} want={want!r}"))


def _row(workspace: Path, task_id: str) -> dict:
    rows = tw.task_history_payload(workspace)["tasks"]
    hit = [r for r in rows if r["id"] == task_id]
    return hit[0] if hit else {}


def _workspace(task_id: str, *, archived_task: bool = False):
    d = Path(tempfile.mkdtemp())
    (d / "tasks").mkdir()
    (d / "tasks" / "archive").mkdir()
    res = d / "results"
    res.mkdir()
    (res / "archive").mkdir()
    parent = d / "tasks" / "archive" if archived_task else d / "tasks"
    (parent / f"{task_id}.txt").write_text(
        f"id: {task_id}\ntimestamp: 2026-08-27T00:00:00Z\ntask: ask\nsource: chat\n")
    return d, res


# --- a torn body is NOT an answer ------------------------------------------
tid = "task-1787800600"
ws, res = _workspace(tid)
(res / f"{tid}.txt").write_bytes("NEW ANSWER \xe2\x80".encode("latin-1"))
check("resolve_result calls a torn holder pending", ltp.resolve_result(res, tid)[0], "pending")
row = _row(ws, tid)
check("history does not report a torn holder as done", row.get("status"), "working")
check("history does not hand out a lossily-decoded body", row.get("result"), "")
check("...and specifically no replacement char", "�" in (row.get("result") or ""), False)
shutil.rmtree(ws, ignore_errors=True)

# --- a whole body IS an answer (the control that must stay green) ----------
tid = "task-1787800601"
ws, res = _workspace(tid)
(res / f"{tid}.txt").write_text("NEW ANSWER — authoritative")
check("resolve_result calls a whole holder ready", ltp.resolve_result(res, tid)[0], "ready")
row = _row(ws, tid)
check("history reports a whole holder done", row.get("status"), "done")
check("...with the body intact", row.get("result"), "NEW ANSWER — authoritative")
shutil.rmtree(ws, ignore_errors=True)

# --- the epoch-suffixed archive layout the private index could not key -----
tid = "task-1787800602"
ws, res = _workspace(tid, archived_task=True)
(res / "archive" / f"{tid}-1787797820.txt").write_text("FLAT ANSWER")
check("resolve_result finds the suffixed archive result", ltp.resolve_result(res, tid)[0], "ready")
row = _row(ws, tid)
check("history finds it too, rather than done-and-empty", row.get("result"), "FLAT ANSWER")
check("...and marks it done", row.get("status"), "done")
shutil.rmtree(ws, ignore_errors=True)

# --- the private index is gone; a second reader is the defect --------------
src = (REPO / "src" / "task_workstreams.py").read_text()
check("no private result index remains in task_workstreams", "_result_index" in src, False)
# Task-file reads may still use errors="replace"; a RESULT read may not.
check("no result path lossily decodes a body",
      'read_text(errors="replace")[:MAX_RESULT_CHARS]' in src, False)
check("_exact_result goes through the readiness owner",
      "resolve_result(\n        workspace / \"results\", task_id)" in src, True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
