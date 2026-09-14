#!/usr/bin/env python3
"""A handler's own terminal exit code outranks the disposition fixed at probe time.

The disposition is recorded when the task is ADMITTED (`acquire_task_claim`, from
the probe's verdict) and `finish_handler_task` branched on that stored value
alone. So a handler that probes 0 -- "I accept this" -- and then fails its real
run had that failure read as "optional handler declined", and the task was
emitted to the unrestricted live core.

Exit 4 is the protocol's "must-handle": the handler saying the core must not
inherit this work. `pool_route_handler.py` returns it from every real-run failure
for exactly that reason, and until this fix the watcher ignored it.

Run: python3 tests/watch-tasks-stream-handler-terminal-rc.test.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def run(real_run_rc: int, probe_rc: int = 0):
    """Drive the real watcher with a handler that probes `probe_rc` then exits
    `real_run_rc`. Returns (emitted-to-core, results-written)."""
    tmp = Path(tempfile.mkdtemp(prefix="term-rc-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)
    h = tmp / "handler.sh"
    h.write_text('#!/bin/sh\n'
                 f'for a in "$@"; do [ "$a" = "--probe" ] && exit {probe_rc}; done\n'
                 f'exit {real_run_rc}\n')
    h.chmod(0o755)
    (ws / "tasks" / "task-demo.txt").write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)
    env.pop("SUTANDO_INSTANCE_ID", None)
    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")],
                         cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    out, t0 = [], time.time()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < 10:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
                if c:
                    out.append(c)
            except Exception:
                pass
            if any("TASK_FILE" in c for c in out):
                break
    finally:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except Exception:
            pass
        p.wait(timeout=5)
    emitted = any("TASK_FILE" in c for c in out)
    published = sorted(q.name for q in (ws / "results").glob("*.txt"))
    return emitted, published


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


emitted, published = run(real_run_rc=4)
check("a must-handle result is NOT emitted to the live core", not emitted,
      "the task reached the unrestricted core despite the handler refusing it")
check("a must-handle result publishes a terminal failure instead", published != [],
      "nothing was published, so the task is neither delivered nor failed")

# Control: treating EVERY failure as must-handle would pass the case above while
# removing the fallback feature, so an ordinary rc=1 must still reach the core.
emitted_one, _ = run(real_run_rc=1)
check("control: an ordinary failure still falls back to the core", emitted_one,
      "the fallback path was removed, not narrowed")

# Control 2: success is not a failure. Without this the first check passes for a
# watcher that never emits anything at all.
emitted_zero, _ = run(real_run_rc=0)
check("control: a successful run emits nothing and needs no failure", not emitted_zero)

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else "PASS — handler terminal rc outranks the probe-time disposition")
sys.exit(1 if FAILURES else 0)
