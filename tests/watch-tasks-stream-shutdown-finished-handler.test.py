"""A signal that cuts the watcher's wait does not un-finish the handler.

run_handler_now() runs the optional task handler as a child and waits on it. A
SIGTERM during that wait fires the cleanup trap before the function resumes, so
the claim it took is still on disk when settle_own_claims_on_shutdown() runs.
The settle used to read every such claim as "never handled" and hand the task
to the live core -- a second delivery of work the handler had already done.

Measured against the real watcher with a stub fswatch and a handler that logs
`handle`, then lingers one second before exiting 0, with SIGTERM sent to the
watcher process ALONE right after `handle` appears:

    parent  -> stdout=[TASK_FILE: task-demo.txt]  stderr: handler interrupted; falling back
    HEAD    -> stdout=[]                          stderr: handler ... had already finished

The whole-group kill is the control that must NOT change: it kills the handler
too, its exit is a signal, the outcome is unknown, and the fallback stays.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
LAST_STDERR = [""]


def run(kill_group, linger="1"):
    tmp = Path(tempfile.mkdtemp(prefix="b4816-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True); (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n"); (b / "fswatch").chmod(0o755)
    log = tmp / "handler.log"; h = tmp / "handler.sh"
    # The work is done the moment `handle` is logged; the linger is the window
    # in which a real router is still tearing down after delivering.
    h.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }; done\n'
                 'echo handle >> %s\nsleep %s\nexit 0\n' % (log, log, linger)); h.chmod(0o755)
    name = "task-demo.txt"
    (ws / "tasks" / name).write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"; env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)
    env.pop("SUTANDO_INSTANCE_ID", None)
    errf = open(tmp / "watcher.err", "w+")
    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"), "--role", "standby",
                          "--inbox", str(ws / "tasks")], cwd=str(REPO), env=env,
                         stdout=subprocess.PIPE, stderr=errf, text=True, start_new_session=True)
    out, t0 = [], time.time()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < 20:
            time.sleep(0.1)
            try:
                c = p.stdout.read()
                if c: out.append(c)
            except Exception: pass
            if log.exists() and "handle" in log.read_text():
                break
        else:
            FAILURES.append("handler never ran"); return [], []
        # The handler has done its work and is still alive inside its linger:
        # the watcher is blocked in `wait` on it. Signal now.
        if kill_group:
            os.killpg(os.getpgid(p.pid), 15)
        else:
            os.kill(p.pid, 15)
        # Everything the settle emits arrives before the process exits.
        p.wait(timeout=15)
        try:
            c = p.stdout.read()
            if c: out.append(c)
        except Exception: pass
    finally:
        try: os.killpg(os.getpgid(p.pid), 9)
        except Exception: pass
        errf.seek(0); LAST_STDERR[0] = errf.read(); errf.close()
    return "".join(out).strip().splitlines(), (log.read_text().split() if log.exists() else [])


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_STDERR[0].strip():
            print("  watcher stderr:")
            for line in LAST_STDERR[0].strip().splitlines()[-12:]:
                print("    " + line)


so, hl = run(kill_group=False)
check("SIGTERM to the watcher alone, handler finishing: the task is NOT handed to the live core",
      hl == ["probe", "handle"] and not any("TASK_FILE" in s for s in so), f"{so} {hl}")
check("...and the settle says why",
      "had already finished" in LAST_STDERR[0], LAST_STDERR[0].strip().splitlines()[-1:] )

so, hl = run(kill_group=True)
check("control: the whole group killed, handler dies with it: the fallback to the live core stays",
      hl == ["probe", "handle"] and any("TASK_FILE" in s for s in so), f"{so} {hl}")
check("...with the interrupted-handler line",
      "handler interrupted" in LAST_STDERR[0], LAST_STDERR[0].strip().splitlines()[-1:])

print(f"watch-tasks-stream-shutdown-finished-handler: {4 - len(FAILURES)}/4 passed")
sys.exit(1 if FAILURES else 0)
