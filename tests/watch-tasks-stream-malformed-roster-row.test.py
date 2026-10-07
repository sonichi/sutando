#!/usr/bin/env python3
"""A malformed roster row must not send a BOUND task to the unrestricted core.

Two suites already cover the halves: `pool-route-handler.test.py` proves the
handler ANSWERS must-handle on a malformed row, and
`watch-tasks-stream-handler-terminal-rc.test.py` proves the watcher HONOURS a
must-handle -- but with a stub handler. Neither composes them, so the pair could
both pass while the real handler and the real watcher disagree about the wire.

This drives the REAL `pool_route_handler.py` from the REAL watcher, which is the
control the reviewer asked for: a valid JSON roster whose worker row is a STRING
reaches the advertisement renderer and used to raise before the fail-closed
classifier ran, escaping as rc 1 -- indistinguishable to the watcher from an
optional decline.

Run: python3 tests/watch-tasks-stream-malformed-roster-row.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
W = "a" * 32
ROOM = "!bound:example.test"
FAILURES: list[str] = []


def run(worker_row, bound=True):
    """Drive the real watcher + real handler. Returns (emitted-to-core, results)."""
    tmp = Path(tempfile.mkdtemp(prefix="malformed-row-"))
    ws = tmp / "ws"
    for d in ("tasks", "results/archive", "state", "deliveries"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"version": 1, "workers": {W: worker_row}, "bindings": {ROOM: W}}))

    feed = tmp / "feed"
    feed.write_text("")
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)

    room = ROOM if bound else "!unbound:example.test"
    (ws / "tasks" / "task-bound.txt").write_text(
        f"id: task-bound\nchannel_id: {room}\nsource: ag2space\n"
        "access_tier: owner\ntask: body\n")

    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_TASK_EVENT_HANDLER"] = str(
        REPO / "skills" / "worker-pool" / "scripts" / "pool_route_handler.py")
    env.pop("SUTANDO_INSTANCE_ID", None)

    p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"), "--role", "standby", "--inbox", str(ws / "tasks")],
                         cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    out, t0 = [], time.time()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < 12:
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
    delivered = sorted(str(q.relative_to(ws)) for q in (ws / "deliveries").rglob("*")
                       if q.is_file())
    return (any("TASK_FILE" in c for c in out), delivered)


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# POSITIVE CONTROL FIRST: an UNBOUND task must reach the core. Without it every
# "not emitted" below also passes for a watcher that emits nothing at all.
emitted_unbound, _ = run({"state": "live"}, bound=False)
check("positive control: an unbound task DOES reach the core", emitted_unbound,
      "the harness never emits, so the not-emitted checks below prove nothing")

emitted, delivered = run("not-a-mapping")
check("a malformed worker row does NOT reach the unrestricted core", not emitted,
      "the bound task was emitted; rc 1 read as an optional decline")
check("and the bound task is still DELIVERED to its worker", delivered != [],
      "nothing in deliveries/: a failed advertisement publish stopped routing")

# A failed advertisement must change nothing observable about routing: same
# delivery as a clean roster, which is the whole point of catching it.
emitted_ok, delivered_ok = run({"state": "live"})
check("control: a well-formed row delivers the same way", not emitted_ok and delivered_ok != [],
      f"clean roster: emitted={emitted_ok} delivered={delivered_ok}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES
      else "PASS — a malformed roster row is fail-closed end to end")
sys.exit(1 if FAILURES else 0)
