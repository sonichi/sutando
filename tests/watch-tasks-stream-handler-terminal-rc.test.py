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


def restart_witness():
    """REVIEW.md 15 for this change: a watcher that is STOPPED and STARTED AGAIN
    takes one probe-0/rc-4 task through to a published terminal failure.

    The watcher is a real process both times -- the same `src/watch-tasks-stream.sh`
    the core runs -- so what is exercised is the shipped path across a restart
    boundary, not a harness standing in for it. Only the workspace and the
    fswatch trigger are synthetic.
    """
    tmp = Path(tempfile.mkdtemp(prefix="term-rc-restart-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n"); (b / "fswatch").chmod(0o755)
    h = tmp / "handler.sh"
    h.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "--probe" ] && exit 0; done\nexit 4\n')
    h.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"; env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)
    env.pop("SUTANDO_INSTANCE_ID", None)

    def start():
        return subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")],
                                cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def stop(p):
        try: os.killpg(os.getpgid(p.pid), 15)
        except Exception: pass
        try: p.wait(timeout=10)
        except Exception: p.kill()

    first = start()
    time.sleep(2.0)                      # let the first generation come up and settle
    first_pid = first.pid
    stop(first)                          # THE RESTART BOUNDARY

    # Written while NO watcher runs, so the restarted process admits it on its own
    # startup sweep; created later, the stub fswatch never fires and nothing runs.
    (ws / "tasks" / "task-restart.txt").write_text("id: task-restart\naccess_tier: owner\ntask: probe\n")
    second = start()
    out, t0 = [], time.time()
    published = []
    try:
        os.set_blocking(second.stdout.fileno(), False)
        while time.time() - t0 < 15:
            time.sleep(0.3)
            try:
                c = second.stdout.read()
                if c: out.append(c)
            except Exception:
                pass
            published = sorted(q.name for q in (ws / "results").glob("*.txt"))
            if published:
                break
    finally:
        stop(second)
    emitted = any("TASK_FILE" in c for c in out)
    body = ""
    if published:
        body = (ws / "results" / published[0]).read_text(errors="replace")[:200]
    return first_pid, second.pid, emitted, published, body



def run_ambiguous_lookup(second_manifest: bool):
    """Two skills declaring the capability makes resolve_task_event_handler
    return rc 2 -- "cannot tell", not "no pool". Temp skill dirs inside the
    REAL checkout (like every other test here uses REPO as cwd), never a
    synthetic repo tree -- the resolver's own dependency chain is the repo's,
    not something a fixture should have to reassemble by hand.
    Returns (emitted-to-core, results-written)."""
    tmp = Path(tempfile.mkdtemp(prefix="term-rc-ambig-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)

    # worker-pool already declares this, so 0 extra IS the single-declarer case;
    # any temp skill makes two -- ambiguous, which is only wanted for the second.
    names = [f"zzz-term-rc-test-{tmp.name}-b"] if second_manifest else []
    made = []
    try:
        for name in names:
            skill_dir = REPO / "skills" / name
            (skill_dir / "scripts").mkdir(parents=True)
            made.append(skill_dir)
            script = skill_dir / "scripts" / "route_handler.py"
            script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
            script.chmod(0o755)
            (skill_dir / "manifest.json").write_text(
                '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/route_handler.py"}}\n')

        (ws / "tasks" / "task-ambig.txt").write_text("id: task-ambig\naccess_tier: owner\ntask: probe\n")
        env = dict(os.environ)
        env["PATH"] = f"{b}:{env['PATH']}"
        env["TMPDIR"] = str(tmp)
        env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
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
                if any("TASK_FILE" in c for c in out) or list((ws / "results").glob("*.txt")):
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
    finally:
        import shutil
        for d in made:
            shutil.rmtree(d, ignore_errors=True)

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

pid1, pid2, emitted_r, published_r, body_r = restart_witness()
print(f"\n  restart witness: watcher pid {pid1} stopped, pid {pid2} started; task arrived after the restart")
print(f"    emitted to the live core: {emitted_r}")
print(f"    published by the restarted watcher: {published_r}")
print(f"    result body: {body_r.strip()[:120]!r}")
check("restart: the restarted watcher does NOT hand the task to the core", not emitted_r)
check("restart: the restarted watcher publishes a terminal failure", published_r != [],
      "no result file, so the task is neither delivered nor failed")


# keweichen's finding on #4472: rc 2 ("cannot tell", two providers declare the
# capability) is not rc 1 ("no provider") and must not take the same fallback.
emitted_ambig, published_ambig = run_ambiguous_lookup(second_manifest=True)
check("an ambiguous lookup does NOT reach the live core", not emitted_ambig,
      "rc 2 was treated the same as rc 1 -- the exact fail-open keweichen found")
check("an ambiguous lookup publishes a terminal failure instead", published_ambig != [],
      "the task was neither delivered nor failed")

# Control: ONE declaring skill resolves cleanly and is not caught by this branch.
emitted_one_decl, _ = run_ambiguous_lookup(second_manifest=False)
check("control: exactly one declaring skill still reaches the live core", emitted_one_decl)

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else "PASS — handler terminal rc outranks the probe-time disposition")
sys.exit(1 if FAILURES else 0)
