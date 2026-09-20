#!/usr/bin/env python3
"""A no-pool install must be inert: no directories, just for starting.

`ensure_dispatch_ready()` (claims dir, fallback receipts dir, a dispatch temp
dir) ran eagerly at watcher startup whenever ANY handler resolved -- and the
worker-pool skill ships a handler unconditionally, whether or not a pool
actually exists. So a plain no-pool install (the common case) created these
directories on every boot, purely because its dormant handler was resolvable,
contradicting the documented invariant that a no-pool install publishes
nothing (keweichen, PR #4472 review).

Driven against the REAL checkout (real worker-pool skill and manifest, as
every install ships it) and the real watcher -- not a synthetic repo tree,
since the defect is specifically about what the SHIPPED skill causes.

Run: python3 tests/watch-tasks-stream-no-pool-no-side-effects.test.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def start_watcher(ws, tmp, b):
    env = dict(os.environ)
    for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_TASKS_DIR", "SUTANDO_WATCHER_CMD",
              "SUTANDO_WORKER_BOOTSTRAP", "SUTANDO_WORKSPACE_DIR", "SUTANDO_CORE_RUNTIME",
              "SUTANDO_CORE_SESSION", "SUTANDO_TMUX_SESSION", "SUTANDO_TMUX_SOCKET",
              "SUTANDO_INBOX_RESOLVER", "SUTANDO_TASK_EVENT_HANDLER"):
        env.pop(k, None)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    return subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")],
                             cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


tmp = Path(tempfile.mkdtemp(prefix="no-pool-"))
ws = tmp / "ws"
(ws / "tasks").mkdir(parents=True)
(ws / "results" / "archive").mkdir(parents=True)
(ws / "state").mkdir()
feed = tmp / "feed"; feed.write_text("")
b = tmp / "bin"; b.mkdir()
(b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
(b / "fswatch").chmod(0o755)

p = start_watcher(ws, tmp, b)
try:
    time.sleep(3.0)   # settle time; no task ever arrives in this scenario
    claims_dir = ws / "state" / "task-event-handler-claims"
    fallbacks_dir = ws / "state" / "task-event-handler-fallbacks"
    dispatch_tmp = list(tmp.glob("sutando-task-dispatch.*"))
    check("no claims directory is created for an install with no pool activity",
          not claims_dir.exists(), f"exists: {list(claims_dir.iterdir()) if claims_dir.exists() else None}")
    check("no fallback-receipts directory is created either",
          not fallbacks_dir.exists())
    check("no dispatch temp directory is created either",
          not dispatch_tmp, f"found: {dispatch_tmp}")
finally:
    stop(p)

# Control: once a real task actually arrives, the machinery a real dispatch
# needs is still created -- the fix is about idle startup, not about tasks.
(ws / "tasks" / "task-demo.txt").write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")
p2 = start_watcher(ws, tmp, b)
out, t0 = [], time.time()
try:
    os.set_blocking(p2.stdout.fileno(), False)
    while time.time() - t0 < 10:
        time.sleep(0.3)
        try:
            c = p2.stdout.read()
            if c:
                out.append(c)
        except Exception:
            pass
        if any("TASK_FILE" in c for c in out):
            break
finally:
    stop(p2)
emitted = any("TASK_FILE" in c for c in out)
check("control: a real task still falls through to the live core (routing unchanged)",
      emitted, "the fix must not change actual routing outcomes, only idle-startup side effects")
check("control: a real task's processing DOES create the claims directory",
      (ws / "state" / "task-event-handler-claims").exists())

print(("FAILED -- " + ", ".join(FAILURES)) if FAILURES
      else "PASS -- a no-pool install is inert at startup; real tasks are unaffected")
sys.exit(1 if FAILURES else 0)
