#!/usr/bin/env python3
"""write_status must never expose a zero-length core-status.json to a reader.

The bug this pins: every historical writer was a shell `>` redirect, which
truncates before it writes. graceful-restart's busy() gate read that empty
window as "idle" and authorised a kill (#3156). The reader was hardened there;
this pins the writer side so the window is not reopened.

Calls the PRODUCTION writer, not a copied temp+rename recipe — a test that
reimplements the thing under test passes while the real writer regresses.
"""
import importlib.util
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("wd", REPO / "src" / "workspace_default.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

failures = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


ws = Path(tempfile.mkdtemp())

print("1. a written record round-trips and lands where readers look")
p = wd.write_status("core-status.json", {"status": "running", "ts": 1}, workspace=ws)
check(p == wd.status_path("core-status.json", ws), "writer targets status_path()")
check(p.read_text().strip().startswith("{"), "record is readable JSON")

print("2. no reader ever observes a zero-length file while writes are in flight")
stop = threading.Event()
empties = []
reads = [0]


def writer():
    i = 0
    while not stop.is_set():
        i += 1
        wd.write_status("core-status.json",
                        {"status": "running", "step": "x" * 200, "ts": i}, workspace=ws)


def reader():
    target = wd.status_path("core-status.json", ws)
    while not stop.is_set():
        try:
            raw = target.read_text()
        except FileNotFoundError:
            empties.append("missing")
            continue
        reads[0] += 1
        if raw.strip() == "":
            empties.append("empty")


threads = [threading.Thread(target=writer), threading.Thread(target=reader),
           threading.Thread(target=reader)]
for t in threads:
    t.daemon = True
    t.start()
stop.wait(3.0)
stop.set()
for t in threads:
    t.join(timeout=5)

check(reads[0] > 500, f"reader sampled enough to be meaningful (got {reads[0]})")
check(not empties, f"no empty/missing reads (got {len(empties)})")

print("3. no temp files are left behind")
strays = [f.name for f in wd.status_path("core-status.json", ws).parent.iterdir()
          if ".tmp" in f.name]
check(not strays, f"no stray temp files ({strays})")

print("4. the shell wrapper carries the pending queue and refreshes state/task-queue.json")
# The real scripts/core-status.sh from a copy of the repo tooling whose workspace_default is
# pinned to a temp workspace: the production writer and queue reader, none of the live workspace.
import json
import os
import shutil
import subprocess

rig = Path(tempfile.mkdtemp())
ws4 = rig / "ws"
(ws4 / "tasks").mkdir(parents=True)
(rig / "scripts").mkdir(); (rig / "src").mkdir()
shutil.copy(REPO / "scripts" / "core-status.sh", rig / "scripts" / "core-status.sh")
shutil.copy(REPO / "scripts" / "python-binary.sh", rig / "scripts" / "python-binary.sh")
(rig / "src" / "workspace_default.py").write_text(
    "import importlib.util\nfrom pathlib import Path\n"
    f"_spec = importlib.util.spec_from_file_location('wd_real', {str(REPO / 'src' / 'workspace_default.py')!r})\n"
    "_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)\n"
    f"WS = Path({str(ws4)!r})\n"
    "def resolve_workspace(migrate=True): return WS\n"
    "def status_path(name, workspace=None): return _m.status_path(name, WS)\n"
    "def write_status(name, payload, workspace=None): return _m.write_status(name, payload, workspace=WS)\n")
(rig / "src" / "task_queue.py").write_text(
    "import importlib.util\n"
    f"_spec = importlib.util.spec_from_file_location('task_queue', {str(REPO / 'src' / 'task_queue.py')!r})\n"
    "_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)\n"
    "pending = _m.pending\nwrite_snapshot = _m.write_snapshot\n")
(ws4 / "tasks" / "task-one.txt").write_text("id: task-one\nsource: ag2space\ntask: hi\n")
(ws4 / "tasks" / "task-cron-1.txt").write_text("id: task-cron-1\ntask: bookkeeping\n")
run = subprocess.run(["bash", str(rig / "scripts" / "core-status.sh"), "running", "probe"],
                     capture_output=True, text=True, cwd=str(rig))
check(run.returncode == 0, f"core-status.sh exits 0 ({run.stderr.strip()[-200:]})")
rec = json.loads((ws4 / "state" / "core-status.json").read_text())
check(rec.get("status") == "running" and rec.get("step") == "probe" and isinstance(rec.get("ts"), int),
      "status, step and ts are still written")
check([t.get("id") for t in rec.get("pending", [])] == ["task-one"],
      f"pending lists the owner queue, bookkeeping excluded ({rec.get('pending')})")
snap = json.loads((ws4 / "state" / "task-queue.json").read_text())
check(snap.get("depth") == 1 and [t["id"] for t in snap["pending"]] == ["task-one"],
      "state/task-queue.json is refreshed by the same write")
run_idle = subprocess.run(["bash", str(rig / "scripts" / "core-status.sh"), "idle"],
                          capture_output=True, text=True, cwd=str(rig))
rec_idle = json.loads((ws4 / "state" / "core-status.json").read_text())
check(run_idle.returncode == 0 and rec_idle.get("status") == "idle" and "pending" in rec_idle,
      "idle carries pending too")

print("5. an unreadable tasks/ still writes the status, carries no pending, and leaves the snapshot alone")
if os.geteuid() == 0:
    print("  skip: root reads any directory, chmod cannot make tasks/ unreadable")
else:
    snap_path = ws4 / "state" / "task-queue.json"
    snap_before = snap_path.read_bytes()
    (ws4 / "tasks").chmod(0)
    try:
        run5 = subprocess.run(["bash", str(rig / "scripts" / "core-status.sh"), "running", "denied"],
                              capture_output=True, text=True, cwd=str(rig))
    finally:
        (ws4 / "tasks").chmod(0o755)
    rec5 = json.loads((ws4 / "state" / "core-status.json").read_text())
    check(run5.returncode == 0 and rec5.get("status") == "running" and rec5.get("step") == "denied",
          f"the liveness write still lands (rc={run5.returncode}, {run5.stderr.strip()[-200:]})")
    check("pending" not in rec5, f"no pending key: a count it could not take is absent, not [] ({rec5.get('pending')})")
    check("pending queue not counted" in run5.stderr and "ermission" in run5.stderr,
          f"the refusal is on stderr ({run5.stderr.strip()[-200:]})")
    check(snap_path.read_bytes() == snap_before, "state/task-queue.json is byte-for-byte the previous snapshot")
shutil.rmtree(rig, ignore_errors=True)

if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("ALL PASS")
