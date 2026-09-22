"""A fallback receipt is instance-local knowledge, not a shared flag.

The receipt says "MY optional handler declined this task". Written to one shared
directory, every OTHER watcher read it as its own and bypassed its handler,
emitting the task straight to its own live core. Measured against the real
watcher with a stub fswatch and a logging handler:

    FOREIGN receipt -> stdout=[TASK_FILE: task-demo.txt]  handler=[probe]
    scoped receipt  -> stdout=[]                          handler=[probe, probe, handle]

The own-receipt cases are the negative control: bypassing on your OWN receipt is
the feature, and a fix that broke it would pass a foreign-receipt test alone.

A pool worker (SUTANDO_INSTANCE_ID set) is a separate case, not a receipt-scope
variant of it: since #4502/#4503, a worker never consults a handler OR a receipt
at all -- its own inbox already is the routing decision, made by whoever
delivered the sentinel there. So a worker bypasses unconditionally, with the
handler never even probed, regardless of which receipt (if any) exists.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []

def run(watcher_instance, receipt_owner, want_state=False):
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
    # stderr is kept: a probe that fails with nothing to read cannot be diagnosed.
    errf = open(tmp / "watcher.err", "w+")
    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"), "--role", "standby", "--inbox", str(ws / "tasks")], cwd=str(REPO),
                         env=env, stdout=subprocess.PIPE, stderr=errf,
                         text=True, start_new_session=True)
    out, t0, seen_state = [], time.time(), set()
    # The sentinel lands only after fswatch is confirmed up, so a loaded runner
    # can take longer than a probe that stops at TASK_FILE; give it a real budget.
    budget = 30 if want_state else 8
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < budget:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
                if c: out.append(c)
            except Exception: pass
            # Sample WHILE the watcher lives: its cleanup trap unlinks the
            # sentinel on exit, so a post-hoc listing is always empty.
            if want_state and not seen_state:
                seen_state.update(q.name for q in (ws / "state").glob("watch-tasks-stream*.pid"))
                if seen_state:
                    print(f"  sentinel landed {time.time() - t0:.1f}s after launch")
            else:
                seen_state.update(q.name for q in (ws / "state").glob("watch-tasks-stream*.pid"))
            # The sweep announces before the sentinel is stamped (the stamp
            # waits for fswatch to be confirmed up), so keep sampling for it.
            if want_state and not seen_state: continue
            if log.exists() and "handle" in log.read_text(): break
            if any("TASK_FILE" in c for c in out): break
    finally:
        try: os.killpg(os.getpgid(p.pid), 15)
        except Exception: pass
        p.wait(timeout=5)
        errf.seek(0); LAST_STDERR[0] = errf.read(); errf.close()
    _res = "".join(out).strip().splitlines(), (log.read_text().split() if log.exists() else [])
    return (sorted(seen_state), _res) if want_state else _res

LAST_STDERR = [""]

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_STDERR[0].strip():
            print("  watcher stderr:")
            for line in LAST_STDERR[0].strip().splitlines()[-20:]:
                print("    " + line)


# Single probe now: no queue between probe and run to guard against a stale
# enqueue-time handler reference (see PR body for the old async shape).
HANDLED = ["probe", "handle"]
BYPASSED = ["probe"]
NO_HANDLER_CALL = []  # a worker never probes: not even a bare "probe" entry

so, hl = run(None, None)
check("no receipt: the handler handles it", hl == HANDLED and not so, f"{so} {hl}")

so, hl = run(None, "default")
check("own receipt: the watcher still bypasses to its core",
      hl == BYPASSED and any("TASK_FILE" in s for s in so), f"{so} {hl}")

so, hl = run("worker-1", "default")
check("a worker bypasses unconditionally, even with someone else's receipt on disk",
      hl == NO_HANDLER_CALL and any("TASK_FILE" in s for s in so), f"{so} {hl}")

so, hl = run("worker-1", "worker-1")
check("a worker bypasses unconditionally on its own receipt too -- the receipt is irrelevant",
      hl == NO_HANDLER_CALL and any("TASK_FILE" in s for s in so), f"{so} {hl}")

so, hl = run("worker-1", None)
check("a worker bypasses unconditionally with NO receipt at all",
      hl == NO_HANDLER_CALL and any("TASK_FILE" in s for s in so), f"{so} {hl}")

# STATE_DIR once re-resolved the CHECKOUT workspace while every other state path
# followed argv-derived WORKSPACE_DIR, so this test deleted a live sentinel.
_canon = Path(subprocess.run(["bash", str(REPO / "scripts/sutando-config.sh"), "workspace"],
                             capture_output=True, text=True).stdout.strip()) / "state" / "watch-tasks-stream.pid"
# READ-ONLY on the canonical path: never seed, never unlink. Seeding-then-
# unlinking deletes the sentinel of a watcher that starts mid-test.
_before = (_canon.exists(), _canon.read_bytes() if _canon.exists() else None)
_scratch_seen, _ = run(None, None, want_state=True)
_after = (_canon.exists(), _canon.read_bytes() if _canon.exists() else None)

# The race-free discriminator: the watcher must have written INTO the argv-derived
# scratch workspace. This fails on the pre-fix source and needs no canonical write.
check("the watcher's sentinel lands in the SCRATCH workspace, not the checkout",
      _scratch_seen == ["watch-tasks-stream.pid"],
      f"sampled while alive, scratch state held {_scratch_seen}")

check("the canonical CHECKOUT sentinel is unchanged (read-only check)",
      _after == _before,
      f"before={_before!r} after={_after!r} -- either this test wrote it, or a real "
      f"watcher started mid-run; both are worth a human look")

_TOTAL = 6
print(f"watch-tasks-stream-fallback-receipt-scope: {_TOTAL - len(FAILURES)}/{_TOTAL} passed")
sys.exit(1 if FAILURES else 0)
