#!/usr/bin/env python3
"""A watcher on a worker's delivery inbox (<ws>/deliveries/<id>) started with no
SUTANDO_INSTANCE_ID behaved as the core: it stamped the CORE's readiness sentinel
with its own pid (so role-present read the live core watcher as absent once that
pid exited), loaded the core's task-event handler, and beat as `core`.

Against a live watcher and real fswatch, instance-less on a delivery inbox:

1. It stamps THAT worker's sentinel with its pid, and says it took the id from the inbox.
2. The core's sentinel, naming a live stand-in for the core watcher, is unchanged.
3. The core's task-event handler is not run; the task is announced to its own session.
4. Its watcher beat registers under the worker's id, not `core`.
5. Control: with the worker's id passed, same sentinel, and the core's still untouched.

Run: python3 tests/watch-tasks-stream-delivery-inbox-sentinel-identity.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.process_group import popen_bound  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WID = "d2571c90f75e4907af9f145b01b71c1f"
ACTOR = "@probe-agent:example.test"
FAILURES: list[str] = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def base_env(ws, instance=None, beat=None):
    env = dict(os.environ)
    for k in ("SUTANDO_TASKS_DIR", "SUTANDO_INSTANCE_ID", "SUTANDO_TASK_EVENT_HANDLER",
              "SUTANDO_INBOX_KIND", "SUTANDO_INBOX_RESOLVER", "SUTANDO_INBOX_RESOLVER_TIMEOUT",
              "SUTANDO_POOL_DELIVERY_SCRIPT", "SUTANDO_WATCHER_BEAT", "SUTANDO_AGENT_ID",
              "AGENT_MXID", "AGENT_ID"):
        env.pop(k, None)
    env["AGENT_ID"] = ACTOR
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    if instance:
        env["SUTANDO_INSTANCE_ID"] = instance
    if beat:
        env["SUTANDO_WATCHER_BEAT"] = str(beat)
    return env


def sentinel_for(ws, instance=None):
    out = subprocess.run([sys.executable, str(REPO / "src" / "util_paths.py"), "watcher-sentinel",
                          str(ws / "state")], env=base_env(ws, instance),
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def start(inbox, ws, outf, errf, instance=None, beat=None):
    return popen_bound(
        ["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", "standby", "--inbox", str(inbox)],
        cwd=str(REPO), env=base_env(ws, instance, beat), stdout=outf, stderr=errf,
        text=True)


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


def wait_for(pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.2)
    return False


def read(p):
    try:
        return p.read_text().strip()
    except OSError:
        return None


tmp = Path(tempfile.mkdtemp(prefix="wts-delivery-instance-"))
ws = tmp / "ws"
inbox = ws / "deliveries" / WID
for d in (ws / "tasks", inbox, ws / "results" / "archive", ws / "state"):
    d.mkdir(parents=True, exist_ok=True)

handler_log = tmp / "handler.log"
handler = tmp / "handler.sh"
handler.write_text(f'#!/bin/sh\necho "handle $*" >> {handler_log}\nexit 0\n')
handler.chmod(0o755)
(ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(handler)}))

beat_log = tmp / "beat.log"
beat = tmp / "beat.py"
beat.write_text(f"import sys\nopen({str(beat_log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")

core_sentinel = sentinel_for(ws)
worker_sentinel = sentinel_for(ws, WID)
assert core_sentinel != worker_sentinel, (core_sentinel, worker_sentinel)
core_standin = subprocess.Popen(["sleep", "120"])
core_sentinel.write_text(f"{core_standin.pid}\n")

try:
    out_path, err_path = tmp / "instanceless.out", tmp / "instanceless.err"
    with open(out_path, "w") as outf, open(err_path, "w") as errf:
        p = start(inbox, ws, outf, errf, beat=beat)
        try:
            ok1 = wait_for(lambda: read(worker_sentinel) == str(p.pid))
            core_mid = read(core_sentinel)
            (inbox / "task-probe.txt").write_text("id: task-probe\naccess_tier: owner\ntask: probe\n")
            announced = wait_for(lambda: "TASK_FILE: task-probe.txt" in (read(out_path) or ""))
            wait_for(lambda: beat_log.exists())
        finally:
            stop(p)
    err = err_path.read_text(errors="replace")
    check("(1) an instance-less watcher on a delivery inbox stamps that worker's sentinel",
          ok1, f"worker sentinel={read(worker_sentinel)!r} pid={p.pid} stderr={err[-600:]!r}")
    check("(1b) it says the id came from the inbox",
          "no SUTANDO_INSTANCE_ID on delivery inbox" in err, f"stderr={err[-600:]!r}")
    check("(2) the core's sentinel still names the live core watcher",
          core_mid == str(core_standin.pid) and read(core_sentinel) == str(core_standin.pid),
          f"core sentinel while running={core_mid!r} after={read(core_sentinel)!r} want {core_standin.pid}")
    check("(3) the core's task-event handler is not run on the worker's task",
          not handler_log.exists(), f"handler log={read(handler_log)!r}")
    check("(3b) the task is announced to its own session",
          announced, f"stdout={read(out_path)!r} stderr={err[-600:]!r}")
    beat_args = read(beat_log) or ""
    check("(4) the watcher beat registers under the worker's id, not core",
          f"--id {WID} " in beat_args + " " and "--id core" not in beat_args, f"beat argv={beat_args!r}")

    out2_path, err2_path = tmp / "worker.out", tmp / "worker.err"
    with open(out2_path, "w") as outf2, open(err2_path, "w") as errf2:
        p2 = start(inbox, ws, outf2, errf2, instance=WID)
        try:
            ok = wait_for(lambda: read(worker_sentinel) == str(p2.pid))
            core_after = read(core_sentinel)
        finally:
            stop(p2)
    err2 = err2_path.read_text(errors="replace")
    check("(5) control: with the worker's id it stamps the worker's own sentinel",
          ok, f"worker sentinel={read(worker_sentinel)!r} pid={p2.pid} stderr={err2[-600:]!r}")
    check("(5b) control: the core's sentinel is untouched by the worker's watcher",
          core_after == str(core_standin.pid), f"core sentinel={core_after!r}")
finally:
    core_standin.kill()
    core_standin.wait()

print(f"watch-tasks-stream-delivery-inbox-sentinel-identity: {TOTAL[0] - len(FAILURES)}/{TOTAL[0]} passed")
for f in FAILURES:
    print(f"  FAILED: {f}")
sys.exit(1 if FAILURES else 0)
