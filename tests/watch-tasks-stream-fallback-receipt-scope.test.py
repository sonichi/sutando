"""A fallback receipt is instance-local knowledge, not a shared flag.

The receipt says "MY optional handler declined this task". Written to one shared
directory, every OTHER watcher read it as its own and bypassed its handler,
emitting the task straight to its own live core. Measured against the real
watcher with a stub fswatch and a logging handler:

    FOREIGN receipt -> stdout=[TASK_FILE: task-demo.txt]  handler=[probe]
    scoped receipt  -> stdout=[]                          handler=[probe, handle]

The own-receipt cases are the negative control: bypassing on your OWN receipt is
the feature, and a fix that broke it would pass a foreign-receipt test alone.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []

def run(watcher_instance, receipt_owner):
    """receipt_owner: None | 'default' | '<instance>' — whose receipt exists."""
    tmp = Path(tempfile.mkdtemp(prefix="b4-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True); (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n"); (b / "fswatch").chmod(0o755)
    log = tmp / "handler.log"; h = tmp / "handler.sh"
    h.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }; done\n'
                 'echo handle >> %s\nexit 0\n' % (log, log)); h.chmod(0o755)
    name = "task-demo.txt"
    (ws / "tasks" / name).write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")
    if receipt_owner is not None:
        env0 = dict(os.environ)
        if receipt_owner != "default": env0["SUTANDO_INSTANCE_ID"] = receipt_owner
        else: env0.pop("SUTANDO_INSTANCE_ID", None)
        d = subprocess.run(["python3", str(REPO/"src/util_paths.py"), "handler-fallbacks-dir",
                            str(ws/"state")], capture_output=True, text=True, env=env0).stdout.strip()
        Path(d).mkdir(parents=True, exist_ok=True)
        (Path(d) / name).write_text(str(ws / "tasks" / name) + "\n")
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"; env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)
    if watcher_instance: env["SUTANDO_INSTANCE_ID"] = watcher_instance
    else: env.pop("SUTANDO_INSTANCE_ID", None)
    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")], cwd=str(REPO),
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, start_new_session=True)
    out, t0 = [], time.time()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < 8:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
                if c: out.append(c)
            except Exception: pass
            if log.exists() and "handle" in log.read_text(): break
            if any("TASK_FILE" in c for c in out): break
    finally:
        try: os.killpg(os.getpgid(p.pid), 15)
        except Exception: pass
        p.wait(timeout=5)
    return "".join(out).strip().splitlines(), (log.read_text().split() if log.exists() else [])

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


HANDLED = ["probe", "handle"]
BYPASSED = ["probe"]

so, hl = run(None, None)
check("no receipt: the handler handles it", hl == HANDLED and not so, f"{so} {hl}")

so, hl = run(None, "default")
check("own receipt: the watcher still bypasses to its core",
      hl == BYPASSED and any("TASK_FILE" in s for s in so), f"{so} {hl}")

so, hl = run("worker-1", "default")
check("a FOREIGN receipt does not bypass this instance's handler",
      hl == HANDLED and not so, f"{so} {hl}")

so, hl = run("worker-1", "worker-1")
check("worker-1 still bypasses on its OWN receipt",
      hl == BYPASSED and any("TASK_FILE" in s for s in so), f"{so} {hl}")

print(f"watch-tasks-stream-fallback-receipt-scope: {4 - len(FAILURES)}/4 passed")
sys.exit(1 if FAILURES else 0)
