#!/usr/bin/env python3
"""get_task_result must find an archived result in EVERY layout agent-api itself
produces — including the flat `archive/<id>-<epoch>.txt` that its own archive
move mints on collision.

Before this fix the lookup scanned only `archive/<month>/<id>.txt`, so a result
archived flat returned a terminal 404: "this task never delivered" for a task
that did. The producer and the locator disagreed inside one file.

Run: python3 tests/agent-api-result-archive-layouts.test.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="result-layouts-"))
    api.TASK_DIR = tmp / "tasks"
    api.RESULT_DIR = tmp / "results"
    api.TASK_DIR.mkdir()
    (api.RESULT_DIR / "archive").mkdir(parents=True)
    return tmp


# --- the defect: the layout agent-api's own archive move produces -------------
fresh()
(api.RESULT_DIR / "archive" / "task-flat-1785976425.txt").write_text("FLAT BODY")
got = api.get_task_result("task-flat")
check("flat archive/<id>-<epoch>.txt is found",
      got is not None and got.get("result") == "FLAT BODY", f"got {got!r}")

fresh()
(api.RESULT_DIR / "archive" / "task-direct.txt").write_text("DIRECT BODY")
got = api.get_task_result("task-direct")
check("direct archive/<id>.txt is found",
      got is not None and got.get("result") == "DIRECT BODY", f"got {got!r}")

# --- regression guards: what already worked must keep working -----------------
fresh()
month = api.RESULT_DIR / "archive" / "2026-08"
month.mkdir()
(month / "task-month.txt").write_text("MONTH BODY")
got = api.get_task_result("task-month")
check("month archive/<YYYY-MM>/<id>.txt still found",
      got is not None and got.get("result") == "MONTH BODY", f"got {got!r}")

fresh()
(api.RESULT_DIR / "task-live.txt").write_text("LIVE BODY")
got = api.get_task_result("task-live")
check("live result still wins",
      got is not None and got.get("result") == "LIVE BODY", f"got {got!r}")

# Live must OUTRANK archive — archival trails delivery, so an archived copy can
# be the stale one. A delegation that reordered these would pass every test above.
fresh()
(api.RESULT_DIR / "task-both.txt").write_text("LIVE WINS")
(api.RESULT_DIR / "archive" / "task-both-1785976425.txt").write_text("STALE ARCHIVE")
got = api.get_task_result("task-both")
check("live OUTRANKS archive when both exist",
      got is not None and got.get("result") == "LIVE WINS", f"got {got!r}")

fresh()
(api.TASK_DIR / "task-pending.txt").write_text("queued")
got = api.get_task_result("task-pending")
check("a queued task reports pending, not completed",
      got is not None and got.get("status") == "pending", f"got {got!r}")

fresh()
check("an absent task is None", api.get_task_result("task-absent") is None)

# --- security: the id gate must still reject traversal ------------------------
fresh()
outside = api.RESULT_DIR.parent / "escaped.txt"
outside.write_text("SHOULD NOT BE READABLE")
got = api.get_task_result("../escaped")
check("traversal id does not read outside the results dir",
      got is None or got.get("result") != "SHOULD NOT BE READABLE", f"got {got!r}")

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("  ALL PASS")
