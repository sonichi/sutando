#!/usr/bin/env python3
"""The task-event handler is declared by writing a config file the watcher
fswatches -- so a handler that appears (or changes) mid-run takes effect on
the NEXT task, with no watcher restart.

Three properties, against a live watcher process and REAL fswatch (no stub --
this is exactly the live-streaming path a stub fswatch never exercises):

1. No config file at start: a task is emitted straight to the live core.
2. The config file is written WHILE the watcher is already running (the same
   atomic tmp-then-rename write pool_roster.publish_task_event_handler() does):
   the VERY NEXT task is routed through the declared handler. No restart.
3. A pool worker (SUTANDO_INSTANCE_ID set) never watches or reads the config
   file at all -- even with one already present, its tasks are always emitted
   directly. Core-only, unconditionally.

Run: python3 tests/watch-tasks-stream-config-hot-reload.test.py
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


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def start_watcher(ws, instance=None):
    env = dict(os.environ)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    if instance:
        env["SUTANDO_INSTANCE_ID"] = instance
    else:
        env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)  # no operator pin -- config file only
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")], cwd=str(REPO),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)


def write_task(ws, name, body="probe"):
    # Rename into place, as every bridge does: an in-place write raises a
    # Created and an Updated event, and the watcher would announce both.
    final = ws / "tasks" / name
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


def read_available(p, out):
    try:
        os.set_blocking(p.stdout.fileno(), False)
        c = p.stdout.read()
        if c:
            out.append(c)
    except Exception:
        pass


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


tmp = Path(tempfile.mkdtemp(prefix="teh-hotreload-"))
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

# (1) No config file yet: a task must reach the live core directly.
p = start_watcher(ws)
out: list[str] = []
try:
    write_task(ws, "task-one.txt")
    ok = wait_for(lambda: (read_available(p, out), any("TASK_FILE" in s for s in out))[1])
    check("(1) no config file: the task is emitted straight to the live core",
          ok and not log.exists(), f"out={out} log_exists={log.exists()}")

    # (2) Publish the handler WHILE this same watcher process is still running --
    # the exact atomic write pool_roster.publish_task_event_handler() performs.
    tmp_cfg = cfg.with_name(f".{cfg.name}.tmp")
    tmp_cfg.write_text(json.dumps({"handler": str(handler)}))
    tmp_cfg.replace(cfg)

    # Room for fswatch's -l 0.5 batching window plus FSEvents latency.
    time.sleep(1.5)
    out2: list[str] = []
    write_task(ws, "task-two.txt")
    ok2 = wait_for(lambda: (read_available(p, out2), log.exists() and "handle" in log.read_text())[1])
    check("(2) config written mid-run: the VERY NEXT task is routed through it, no restart",
          ok2 and log.exists() and "handle" in log.read_text(),
          f"out2={out2} log={log.read_text() if log.exists() else None}")
finally:
    stop(p)

# (3) cfg still names `handler` from step (2); log content before this
# section is the control -- must be byte-identical after, proving no call.
log_before = log.read_text() if log.exists() else ""
p2 = start_watcher(ws, instance="worker-1")
out3: list[str] = []
try:
    write_task(ws, "task-three.txt")
    ok3 = wait_for(lambda: (read_available(p2, out3), any("TASK_FILE" in s for s in out3))[1])
    log_after = log.read_text() if log.exists() else ""
    check("(3) a worker never reads an already-present config file -- bypasses unconditionally",
          ok3 and log_after == log_before,
          f"out3={out3} log_before={log_before!r} log_after={log_after!r}")
finally:
    stop(p2)

print(f"watch-tasks-stream-config-hot-reload: {3 - len(FAILURES)}/3 passed")
for f in FAILURES:
    print(f"  FAILED: {f}")
sys.exit(1 if FAILURES else 0)
