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

if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("ALL PASS")
