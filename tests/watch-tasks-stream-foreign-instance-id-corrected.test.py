#!/usr/bin/env python3
"""A --force-restart (or any start) issued from INSIDE another instance's own
session inherits that instance's SUTANDO_INSTANCE_ID in its environment, even
when --inbox explicitly names the CORE's own canonical <ws>/tasks. Before this
fix, src/watch-tasks-stream.sh trusted that inherited env unconditionally: it
watched the right directory but believed itself to be that other worker's own
inbox-watcher (#4502's rule: a worker's own inbox IS the routing decision), so
it never loaded the core's task-event-handler config and silently stopped
dispatching anything -- the exact production incident of 2026-09-30 (pid 1184:
argv correctly said `--role session --inbox <core's workspace>/tasks`, env
said SUTANDO_INSTANCE_ID=<a different worker's id>).

Two properties, against a live watcher and real fswatch:

1. A foreign SUTANDO_INSTANCE_ID inherited while watching the core's own
   <ws>/tasks is corrected: the watcher still loads an already-present
   task-event-handler config and routes through it, exactly as an unset env
   would.
2. The correction is visible: a stderr line names the mismatch.

Control: the SAME setup with the real worker shape (<ws>/deliveries/<id>) is
left alone -- the identity is not corrected away, so a worker's own inbox
still never reads the config (pinned by
tests/watch-tasks-stream-config-hot-reload.test.py property 3).

Run: python3 tests/watch-tasks-stream-foreign-instance-id-corrected.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
LAST_STDERR = [""]


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_STDERR[0].strip():
            print("  watcher stderr:")
            for line in LAST_STDERR[0].strip().splitlines()[-20:]:
                print("    " + line)


def start_watcher(inbox, errf, instance=None, role="standby"):
    env = dict(os.environ)
    env["SUTANDO_RESULTS_DIR"] = str(inbox.parent / "results")
    if instance:
        env["SUTANDO_INSTANCE_ID"] = instance
    else:
        env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", role, "--inbox", str(inbox)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=errf,
        text=True, start_new_session=True)


def write_task(inbox, name, body="probe"):
    final = inbox / name
    tmp = final.with_name(f".{name}.tmp")
    tmp.write_text(f"id: {name}\naccess_tier: owner\ntask: {body}\n")
    tmp.replace(final)


def wait_for(pred, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.2)
    return False


def fswatch_live(p):
    r = subprocess.run(["pgrep", "-P", str(p.pid), "-x", "fswatch"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def wait_for_fswatch(p):
    if not wait_for(lambda: fswatch_live(p), timeout=15):
        raise SystemExit("watcher never started fswatch")


def read_available(p, out):
    try:
        os.set_blocking(p.stdout.fileno(), False)
        c = p.stdout.read()
        if c:
            out.append(c)
    except Exception:
        pass


def snapshot_stderr(errf_path):
    return errf_path.read_text(errors="replace") if errf_path.exists() else ""


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


tmp = Path(tempfile.mkdtemp(prefix="teh-foreign-instance-"))
ws = tmp / "ws"
(ws / "tasks").mkdir(parents=True)
(ws / "results" / "archive").mkdir(parents=True)
(ws / "state").mkdir()
log = tmp / "handler.log"
handler = tmp / "handler.sh"
handler.write_text(
    '#!/bin/sh\n'
    'for a in "$@"; do [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }; done\n'
    'echo handle >> %s\nexit 0\n' % (log, log))
handler.chmod(0o755)

cfg = ws / "state" / "task-event-handler.json"
cfg.write_text(json.dumps({"handler": str(handler)}))

# (1)+(2): the core's own <ws>/tasks, but SUTANDO_INSTANCE_ID carries a
# foreign worker's id -- exactly the --force-restart-from-another-session
# incident.
errf_path = tmp / "watcher.err"
errf = open(errf_path, "w")
p = start_watcher(ws / "tasks", errf, instance="d2571c90f75e4907af9f145b01b71c1f")
out: list[str] = []
try:
    wait_for_fswatch(p)
    write_task(ws / "tasks", "task-one.txt")
    ok = wait_for(lambda: (read_available(p, out), log.exists() and "handle" in log.read_text())[1])
    LAST_STDERR[0] = snapshot_stderr(errf_path)
    check("(1) a foreign inherited SUTANDO_INSTANCE_ID on the core's own inbox is "
          "corrected: the already-present handler still runs",
          ok and log.exists() and "handle" in log.read_text(),
          f"out={out} log={log.read_text() if log.exists() else None}")
    check("(2) the correction is logged to stderr",
          "set while serving the core's own inbox" in LAST_STDERR[0],
          f"stderr={LAST_STDERR[0]!r}")
finally:
    stop(p)
    errf.close()

print(f"watch-tasks-stream-foreign-instance-id-corrected: {2 - len(FAILURES)}/2 passed")
for f in FAILURES:
    print(f"  FAILED: {f}")
sys.exit(1 if FAILURES else 0)
