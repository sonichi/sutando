#!/usr/bin/env python3
"""A task admitted by one probe must not run without a second one at spawn time.

`dispatch_task` probes once to decide whether to queue at all. `drain_dispatch_queue`
re-resolves the handler at spawn time -- "a receipt may outlive the handler that
queued it" -- but until this fix invoked whatever it resolved unconditionally, with
no second probe. A provider that would have declined ran anyway.

No saturation or timing race is needed to observe this: for a single task with a
free slot, `dispatch_task`'s enqueue-time probe and `drain_dispatch_queue`'s
spawn-time resolution happen synchronously, one after the other, in the same
process. A handler that answers its FIRST probe call differently from its SECOND
exercises exactly the two call sites this fix adds a check between.

Run: python3 tests/watch-tasks-stream-drain-reprobe.test.py
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


def run(second_probe_rc: int, real_rc: int = 0, first_probe_rc: int = 4):
    """A handler whose 1st probe call returns `first_probe_rc` and whose 2nd
    probe call returns `second_probe_rc`. Returns (real-invocation-happened,
    published-result-body, TASK_FILE-emitted-to-the-core)."""
    tmp = Path(tempfile.mkdtemp(prefix="drain-reprobe-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"; feed.write_text("")
    b = tmp / "bin"; b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)

    calls = tmp / "probe-calls"
    ran = tmp / "real-run-happened"
    h = tmp / "handler.py"
    h.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "if '--probe' in sys.argv:\n"
        "    n = int(calls.read_text()) if calls.exists() else 0\n"
        "    calls.write_text(str(n + 1))\n"
        f"    sys.exit({first_probe_rc} if n == 0 else {second_probe_rc})\n"
        f"pathlib.Path({str(ran)!r}).write_text('ran')\n"
        f"sys.exit({real_rc})\n")
    h.chmod(0o755)
    (ws / "tasks" / "task-demo.txt").write_text("id: task-demo\naccess_tier: owner\ntask: probe\n")

    env = dict(os.environ)
    for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_TASKS_DIR", "SUTANDO_WATCHER_CMD",
              "SUTANDO_WORKER_BOOTSTRAP", "SUTANDO_WORKSPACE_DIR", "SUTANDO_CORE_RUNTIME",
              "SUTANDO_CORE_SESSION", "SUTANDO_TMUX_SESSION", "SUTANDO_TMUX_SOCKET",
              "SUTANDO_INBOX_RESOLVER"):
        env.pop(k, None)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(h)

    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks")],
                         cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    out = []
    t0 = time.time()
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
            if ran.exists() or list((ws / "results").glob("*.txt")) or any("TASK_FILE" in c for c in out):
                break
    finally:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except Exception:
            pass
        p.wait(timeout=5)

    published = list((ws / "results").glob("*.txt"))
    body = published[0].read_text(errors="replace") if published else ""
    calls_made = int(calls.read_text()) if calls.exists() else 0
    emitted = any("TASK_FILE" in c for c in out)
    return ran.exists(), body, calls_made, emitted


# The bug: admitted at enqueue (probe 1 -> 4), then a DIFFERENT verdict at spawn
# time (probe 2 -> 3, an ordinary decline) ran anyway because nothing re-checked.
ran, body, calls_made, _ = run(second_probe_rc=3)
print(f"  probe called {calls_made} time(s); real invocation ran: {ran}")
print(f"  result body: {body.strip()[:160]!r}")
check("the probe is called a second time before spawning", calls_made == 2,
      f"only {calls_made} call(s) -- the drain-time re-probe never ran")
check("a handler that declines its second probe never performs the real run", not ran,
      "it ran anyway -- the exact bug keweichen found")
check("a terminal failure is published instead (must-handle disposition)", bool(body),
      "the task was neither run nor failed -- silently dropped")

# Control: if the second probe agrees (still 4), the real run proceeds exactly
# as before this fix -- the re-probe must not block the ordinary case.
ran2, body2, calls2, _ = run(second_probe_rc=4, real_rc=4)
check("control: an unchanged admitting handler still runs for real", ran2)

# The fallback (optional) disposition side: admitted at rc 0, declines at
# re-probe. Disposition is "fallback", so the core is correct here, not a drop.
ran3, body3, calls3, emitted3 = run(first_probe_rc=0, second_probe_rc=3)
check("a fallback provider that declines its re-probe never runs for real", not ran3)
check("it falls back to the live core instead (fallback disposition, not must-handle)",
      emitted3, "neither run nor emitted -- the fallback case must not be treated as a refusal")

print(("FAILED -- " + ", ".join(FAILURES)) if FAILURES else "PASS -- the drain re-probes before spawning, not just at enqueue")
sys.exit(1 if FAILURES else 0)
