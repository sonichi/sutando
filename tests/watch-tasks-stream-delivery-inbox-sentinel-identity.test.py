#!/usr/bin/env python3
"""A watcher on a worker's delivery inbox (<ws>/deliveries/<id>) started with no
SUTANDO_INSTANCE_ID resolved its sentinel as the core's: it stamped the CORE's
readiness sentinel with its own pid, so role-present read the live core watcher
as absent once that pid exited.

Against a live watcher and real fswatch:

1. Instance-less, on a delivery inbox: the watcher stamps THAT worker's sentinel
   with its own pid, and says it derived the id from the inbox.
2. The core's sentinel, naming a live stand-in for the core watcher, is unchanged.
3. Control: the same inbox WITH the worker's id starts, stamps the worker's own
   sentinel with its pid, and still leaves the core's sentinel unchanged.

Run: python3 tests/watch-tasks-stream-delivery-inbox-sentinel-identity.test.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WID = "d2571c90f75e4907af9f145b01b71c1f"
ACTOR = "@probe-agent:example.test"
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def base_env(ws, instance=None):
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
    return env


def sentinel_for(ws, instance=None):
    out = subprocess.run([sys.executable, str(REPO / "src" / "util_paths.py"), "watcher-sentinel",
                          str(ws / "state")], env=base_env(ws, instance),
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def start(inbox, ws, errf, instance=None):
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", "standby", "--inbox", str(inbox)],
        cwd=str(REPO), env=base_env(ws, instance), stdout=subprocess.DEVNULL, stderr=errf,
        text=True, start_new_session=True)


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

core_sentinel = sentinel_for(ws)
worker_sentinel = sentinel_for(ws, WID)
assert core_sentinel != worker_sentinel, (core_sentinel, worker_sentinel)
core_standin = subprocess.Popen(["sleep", "120"])
core_sentinel.write_text(f"{core_standin.pid}\n")

try:
    errf_path = tmp / "instanceless.err"
    with open(errf_path, "w") as errf:
        p = start(inbox, ws, errf)
        try:
            ok1 = wait_for(lambda: read(worker_sentinel) == str(p.pid))
            core_mid = read(core_sentinel)
        finally:
            stop(p)
    err = errf_path.read_text(errors="replace")
    check("(1) an instance-less watcher on a delivery inbox stamps that worker's sentinel",
          ok1, f"worker sentinel={read(worker_sentinel)!r} pid={p.pid} stderr={err[-600:]!r}")
    check("(1b) it says the id came from the inbox",
          "no SUTANDO_INSTANCE_ID on delivery inbox" in err, f"stderr={err[-600:]!r}")
    check("(2) the core's sentinel still names the live core watcher",
          core_mid == str(core_standin.pid) and read(core_sentinel) == str(core_standin.pid),
          f"core sentinel while running={core_mid!r} after={read(core_sentinel)!r} want {core_standin.pid}")

    errf2_path = tmp / "worker.err"
    with open(errf2_path, "w") as errf2:
        p2 = start(inbox, ws, errf2, instance=WID)
        try:
            ok = wait_for(lambda: read(worker_sentinel) == str(p2.pid))
            core_after = read(core_sentinel)
        finally:
            stop(p2)
    err2 = errf2_path.read_text(errors="replace")
    check("(3) control: with the worker's id it stamps the worker's own sentinel",
          ok, f"worker sentinel={read(worker_sentinel)!r} pid={p2.pid} stderr={err2[-600:]!r}")
    check("(3b) control: the core's sentinel is untouched by the worker's watcher",
          core_after == str(core_standin.pid), f"core sentinel={core_after!r}")
finally:
    core_standin.kill()
    core_standin.wait()

print(f"watch-tasks-stream-delivery-inbox-sentinel-identity: {5 - len(FAILURES)}/5 passed")
for f in FAILURES:
    print(f"  FAILED: {f}")
sys.exit(1 if FAILURES else 0)
