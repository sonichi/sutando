"""A signal that cuts the watcher's wait does not un-finish the handler, and does
not hide how it finished either.

run_handler_now() runs the optional task handler as a child and waits on it. A
SIGTERM during that wait fires the cleanup trap before the function resumes, so
the claim it took is still on disk when settle_own_claims_on_shutdown() runs.
The settle used to read every such claim as "never handled" and hand the task
to the live core -- a second delivery of work the handler had already done.

Measured against the real watcher with a stub fswatch and a handler that logs
`handle`, lingers, then exits with a chosen code; SIGTERM to the watcher pid
alone, or to its whole process group (the notifiers' stop), right after `handle`:

    parent, rc 0, pid TERM   -> stdout=[TASK_FILE: task-demo.txt]  (the duplicate)
    HEAD,   rc 0, pid TERM   -> stdout=[]   stderr: handler ... had already finished
    HEAD,   rc 4, pid TERM   -> stdout=[]   a terminal failure is published instead
    HEAD,   rc 0, group TERM -> stdout=[TASK_FILE: ...]  the handler died too: unknown, so the fallback stays
    HEAD,   TERM-resistant handler, group TERM, RUN_TIMEOUT=2
                             -> the watcher exits within the settle grace, publishing the
                                interrupted failure the retry test already expects; no unbounded wait

The env is built from the clean fixture: a live core or worker shell carries
SUTANDO_* names that would point this test at real state.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

FAILURES: list[str] = []
LAST_STDERR = [""]
TREES: list[Path] = []
STATE = [{}]


def run(kill_group, rc=0, linger="1", resistant=False, run_timeout=None, want_results=False):
    tmp = Path(tempfile.mkdtemp(prefix="b4816-")); TREES.append(tmp)
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True); (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n"); (b / "fswatch").chmod(0o755)
    log = tmp / "handler.log"; h = tmp / "handler.sh"
    # The work is done the moment `handle` is logged; the linger is the window
    # in which a real router is still tearing down after delivering.
    tail = "trap '' TERM\nsleep 30\n" if resistant else f"sleep {linger}\n"
    h.write_text('#!/bin/bash\nfor a in "$@"; do [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }; done\n'
                 'echo handle >> %s\n%sexit %d\n' % (log, log, tail, rc)); h.chmod(0o755)
    name = "task-demo.txt"
    (ws / "tasks" / name).write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")
    env = clean_env()
    env["PATH"] = f"{b}:{env['PATH']}"; env["TMPDIR"] = str(tmp)
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)
    if run_timeout is not None:
        env["SUTANDO_HANDLER_RUN_TIMEOUT"] = str(run_timeout)
    errf = open(tmp / "watcher.err", "w+")
    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"), "--role", "standby",
                          "--inbox", str(ws / "tasks")], cwd=str(REPO), env=env,
                         stdout=subprocess.PIPE, stderr=errf, text=True, start_new_session=True)
    out, t0, elapsed = [], time.time(), None
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
            FAILURES.append("handler never ran"); return [], [], None, []
        # The handler has done its work and is still alive inside its linger:
        # the watcher is blocked in `wait` on it. Signal now.
        t1 = time.time()
        if kill_group:
            os.killpg(os.getpgid(p.pid), 15)
        else:
            os.kill(p.pid, 15)
        # Everything the settle emits arrives before the process exits.
        p.wait(timeout=25)
        elapsed = time.time() - t1
        try:
            c = p.stdout.read()
            if c: out.append(c)
        except Exception: pass
    finally:
        try: os.killpg(os.getpgid(p.pid), 9)
        except Exception: pass
        errf.seek(0); LAST_STDERR[0] = errf.read(); errf.close()
    published = sorted(q.name for q in (ws / "results").glob("*.txt")) if want_results else []
    # What the settle leaves behind is part of the verdict: no claim, no sentinel, and a
    # fallback receipt only on the path that really handed the task to the core.
    STATE[0] = {"claims": [q.name for q in (ws / "state" / "task-event-handler-claims").glob("*") if not q.name.startswith(".")]
                if (ws / "state" / "task-event-handler-claims").exists() else [],
                "sentinels": [q.name for q in (ws / "state").glob("watch-tasks-stream*.pid")],
                "receipt": any((ws / "state").rglob("task-event-handler-fallbacks/task-demo.txt"))}
    return "".join(out).strip().splitlines(), (log.read_text().split() if log.exists() else []), elapsed, published


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_STDERR[0].strip():
            print("  watcher stderr:")
            for line in LAST_STDERR[0].strip().splitlines()[-12:]:
                print("    " + line)


try:
    so, hl, _, _ = run(kill_group=False)
    check("SIGTERM to the watcher alone, handler finishing with 0: the task is NOT handed to the live core",
          hl == ["probe", "handle"] and not any("TASK_FILE" in s for s in so), f"{so} {hl}")
    check("...and the settle says why",
          "had already finished" in LAST_STDERR[0], LAST_STDERR[0].strip().splitlines()[-1:])
    check("...leaving no claim, no sentinel, and no fallback receipt",
          STATE[0] == {"claims": [], "sentinels": [], "receipt": False}, str(STATE[0]))

    so, hl, _, pub = run(kill_group=False, rc=4, want_results=True)
    check("handler finishing with 4 (must-handle): NOT handed to the live core",
          hl == ["probe", "handle"] and not any("TASK_FILE" in s for s in so), f"{so} {hl}")
    check("...a terminal failure is published instead", pub != [] and "exit 4" in LAST_STDERR[0],
          f"{pub} {LAST_STDERR[0].strip().splitlines()[-1:]}")

    so, hl, _, _ = run(kill_group=True)
    check("control: the whole group killed, handler dies with it: the fallback to the live core stays",
          hl == ["probe", "handle"] and any("TASK_FILE" in s for s in so), f"{so} {hl}")
    check("...with the interrupted-handler line",
          "handler interrupted" in LAST_STDERR[0], LAST_STDERR[0].strip().splitlines()[-1:])
    check("...and the receipt that marks the hand-off, no claim, no sentinel",
          STATE[0] == {"claims": [], "sentinels": [], "receipt": True}, str(STATE[0]))

    so, hl, elapsed, pub = run(kill_group=True, resistant=True, run_timeout=2, want_results=True)
    check("a TERM-resistant handler under the group stop: the watcher still exits, within the settle grace",
          elapsed is not None and elapsed < 10, f"elapsed={elapsed}")
    check("...as a timeout: no hand-off to the live core, a terminal failure published",
          not any("TASK_FILE" in s for s in so) and pub != [] and "still running at shutdown" in LAST_STDERR[0],
          f"{so} {pub} {LAST_STDERR[0].strip().splitlines()[-1:]}")
    check("...leaving no claim, no sentinel, and no fallback receipt",
          STATE[0] == {"claims": [], "sentinels": [], "receipt": False}, str(STATE[0]))
finally:
    for t in TREES:
        shutil.rmtree(t, ignore_errors=True)

print(f"watch-tasks-stream-shutdown-finished-handler: {11 - len(FAILURES)}/11 passed")
sys.exit(1 if FAILURES else 0)
