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

import errno
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

# Only ENOENT is absence. is_dir()/exists() report a FAILED stat as False, so an
# unreadable recipient or sentinel would read as "nobody holds it".
import os as _os
if _os.geteuid() == 0:
    print("  skip root cannot be denied a read")
else:
    for what, denied in (("recipient directory", "deliveries/worker-1"),
                         ("deliveries/ itself", "deliveries")):
        wsp = _ws()
        _task(wsp, "task-eee2")
        (wsp / "deliveries" / "worker-1").mkdir()
        (wsp / "deliveries" / "worker-1" / "task-eee2.txt").write_text("", encoding="utf-8")
        check(holder_of(wsp, "task-eee2") == "worker-1", f"control: readable, {what} reports the holder")
        (wsp / denied).chmod(0o000)
        try:
            holder_of(wsp, "task-eee2")
            held = "returned instead of raising"
        except PermissionError:
            held = None
        finally:
            (wsp / denied).chmod(0o755)
        check(held is None, f"an unreadable {what} propagates — it is not 'nobody holds it'")

    # Path.is_dir() also swallows ELOOP, so a symlink-loop recipient reads as
    # "not a directory" and vanishes from the scan. Only ENOENT is absence.
    wsl = _ws()
    _task(wsl, "task-hhh")
    _loop = wsl / "deliveries" / "loopy"
    _os.symlink(_loop, _loop)
    try:
        holder_of(wsl, "task-hhh")
        looped = "returned instead of raising"
    except OSError as exc:
        looped = None if exc.errno == errno.ELOOP else f"wrong errno {exc.errno}"
    check(looped is None, "a recipient whose stat fails with ELOOP propagates, not skipped as absent")
    _loop.unlink()
    check(holder_of(wsl, "task-hhh") is None, "control: with that entry gone the same tree is simply unheld")


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

# In-process too: a subprocess run exercises the held branch but no coverage
# tracer follows it, so the measured suite would report those lines unhit.
import importlib.util as _ilu
import contextlib as _ctx
import io as _io
_spec = _ilu.spec_from_file_location("unanswered_tasks", ROOT / "scripts" / "unanswered-tasks.py")
_ut = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ut)
_err = _io.StringIO()
with _ctx.redirect_stderr(_err):
    _rows = _ut.unanswered(ws5, min_age_sec=120.0)
check(_rows == [], "unanswered() in-process: a held task yields no rows")
check("held by worker-2" in _err.getvalue(), "and names the holder on stderr from the same call")
_ws7 = _ws()
_task(_ws7, "task-ggg")
check([r[0] for r in _ut.unanswered(_ws7, min_age_sec=120.0)] == ["task-ggg"],
      "and an unheld task still yields its row, so the skip is the sentinel and not the loop")

# An unreadable deliveries/ is a THIRD answer, and must not share an exit code
# with "the core owes a reply" — the caller cannot tell them apart.
ws6 = _ws()
_task(ws6, "task-fff")
(ws6 / "deliveries" / "worker-3").mkdir()
(ws6 / "deliveries" / "worker-3" / "task-fff.txt").write_text("", encoding="utf-8")
import os as _os
if _os.geteuid() == 0:
    print("  skip root cannot be denied a read")
else:
    (ws6 / "deliveries").chmod(0o000)
    try:
        r3 = subprocess.run([sys.executable, str(ROOT / "scripts" / "unanswered-tasks.py"),
                             "--workspace", str(ws6)], capture_output=True, text=True)
    finally:
        (ws6 / "deliveries").chmod(0o755)
    check(r3.returncode == 2, "an unreadable deliveries/ exits 2 (cannot decide), never 1")
    check("cannot decide" in r3.stderr, "and says so on stderr rather than raising a traceback")
    check("Traceback" not in r3.stderr, "no traceback: the CLI owns its own failure mode")
    # The same tree, readable, is the control that proves the 2 came from the mode bits.
    r4 = subprocess.run([sys.executable, str(ROOT / "scripts" / "unanswered-tasks.py"),
                         "--workspace", str(ws6)], capture_output=True, text=True)
    check(r4.returncode == 0, "readable again, the held task is 0 — the 2 was the permission, not the tree")

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
