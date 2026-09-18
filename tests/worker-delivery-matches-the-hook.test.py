#!/usr/bin/env python3
"""The core must not report a task the router already delegated.

Two guards answer "is this still the core's to report?" — the Stop hook in
shell (it must run without an interpreter) and src/worker_delivery.py for
Python callers. This suite pins the behaviour AND pins the two to the same
sentinel suffix set, because the failure mode is silent: the copy nobody
re-reads is the one that hands a worker's task back to the core.

Run: python3 tests/worker-delivery-matches-the-hook.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from worker_delivery import SENTINEL_SUFFIXES, holder_of  # noqa: E402

FAILED: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILED.append(what)


def _ws() -> Path:
    ws = Path(tempfile.mkdtemp())
    (ws / "tasks").mkdir()
    (ws / "results").mkdir()
    (ws / "deliveries").mkdir()
    return ws


def _task(ws: Path, tid: str, age_sec: float = 3600) -> Path:
    f = ws / "tasks" / f"{tid}.txt"
    f.write_text(f"id: {tid}\nsource: ag2space\ntask: work\n", encoding="utf-8")
    old = time.time() - age_sec
    import os
    os.utime(f, (old, old))
    return f


print("worker_delivery.holder_of")
ws = _ws()
_task(ws, "task-aaa")
check(holder_of(ws, "task-aaa") is None, "nobody holds an undelegated task")

for suffix in SENTINEL_SUFFIXES:
    ws2 = _ws()
    _task(ws2, "task-bbb")
    (ws2 / "deliveries" / "worker-1").mkdir()
    (ws2 / "deliveries" / "worker-1" / f"task-bbb{suffix}").write_text("", encoding="utf-8")
    check(holder_of(ws2, "task-bbb") == "worker-1", f"a '{suffix}' sentinel marks the task held")

ws3 = _ws()
_task(ws3, "task-ccc")
(ws3 / "deliveries" / "worker-1").mkdir()
(ws3 / "deliveries" / "worker-1" / "task-OTHER.txt").write_text("", encoding="utf-8")
check(holder_of(ws3, "task-ccc") is None, "another task's sentinel does not mark this one held")

ws4 = Path(tempfile.mkdtemp())
check(holder_of(ws4, "task-ddd") is None, "an absent deliveries/ means nobody holds it")

print("unanswered-tasks.py honours the sentinel")
ws5 = _ws()
_task(ws5, "task-eee")
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "unanswered-tasks.py"),
                    "--workspace", str(ws5)], capture_output=True, text=True)
check(r.returncode == 1, "an unheld task with no result still exits 1 (the guard still guards)")
check("task-eee" in r.stdout, "and it is named on stdout")

(ws5 / "deliveries" / "worker-2").mkdir()
(ws5 / "deliveries" / "worker-2" / "task-eee.txt").write_text("", encoding="utf-8")
r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "unanswered-tasks.py"),
                     "--workspace", str(ws5)], capture_output=True, text=True)
check(r2.returncode == 0, "the SAME task, once a worker holds it, exits 0 — not the core's to answer")
check("task-eee" not in r2.stdout, "and it is no longer reported as unanswered")
check("held by worker-2" in r2.stderr, "but it is still visible on stderr, so a stuck holder is not hidden")

print("the two guards agree on the suffix set")
hook = (ROOT / "src" / "check-pending-tasks.sh").read_text(encoding="utf-8")
m = re.search(r"sentinel_task_id\(\)\s*\{(.*?)\n\}", hook, re.S)
check(m is not None, "the hook still defines sentinel_task_id()")
if m:
    hook_suffixes = set(re.findall(r"\*(\.[a-z]+)\)", m.group(1)))
    check(hook_suffixes == set(SENTINEL_SUFFIXES),
          f"hook {sorted(hook_suffixes)} == module {sorted(SENTINEL_SUFFIXES)}")

print(f"\n{'FAILED: ' + '; '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)
