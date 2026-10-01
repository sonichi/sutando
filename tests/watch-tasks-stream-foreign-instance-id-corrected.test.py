#!/usr/bin/env python3
"""A --force-restart (or any start) issued from INSIDE another instance's own
session inherits that instance's SUTANDO_INSTANCE_ID in its environment, even
when --inbox explicitly names the CORE's own canonical <ws>/tasks. Before this
fix, src/watch-tasks-stream.sh trusted that inherited env unconditionally: it
watched the right directory but believed itself to be that other worker's own
inbox-watcher (a worker's own inbox IS the routing decision), so it never
loaded the core's task-event-handler config and silently stopped dispatching
anything it saw.

Five properties, against a live watcher and real fswatch:

1. A foreign SUTANDO_INSTANCE_ID inherited while watching the core's own
   <ws>/tasks is corrected: the watcher still loads an already-present
   task-event-handler config and routes through it, exactly as an unset env
   would.
2. The correction is visible: a stderr line names the mismatch.
3. The full inherited worker-routing env (not just the instance id) is
   cleared: a real worker-shaped SUTANDO_INBOX_RESOLVER left set would
   otherwise reject every plain task body on its own.
4. Control: a worker inbox whose basename happens to be "tasks" (not the
   canonical <ws>/tasks by realpath) keeps its identity -- the handler config
   is correctly NOT read (pinned by
   tests/watch-tasks-stream-config-hot-reload.test.py property 3).
4b. No foreign-identity correction is logged for that real worker inbox.

Run: python3 tests/watch-tasks-stream-foreign-instance-id-corrected.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
LAST_STDERR = [""]


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
        if LAST_STDERR[0].strip():
            print("  watcher stderr:")
            for line in LAST_STDERR[0].strip().splitlines()[-20:]:
                print("    " + line)


def start_watcher(inbox, errf, instance=None, role="standby", extra_env=None, workspace=None):
    # Pin workspace/tasks-dir env explicitly: dict(os.environ) can inherit a
    # REAL SUTANDO_WORKSPACE_DIR from a live worker shell running this test.
    ws = workspace if workspace else inbox.parent
    env = dict(os.environ)
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env.pop("SUTANDO_TASKS_DIR", None)
    if instance:
        env["SUTANDO_INSTANCE_ID"] = instance
    else:
        env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    for k in ("SUTANDO_INBOX_KIND", "SUTANDO_INBOX_RESOLVER", "SUTANDO_INBOX_RESOLVER_TIMEOUT",
              "SUTANDO_POOL_DELIVERY_SCRIPT"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", role, "--inbox", str(inbox)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=errf,
        text=True, start_new_session=True)


def write_task(inbox, name, body="probe"):
    final = inbox / name
    tmp = final.with_name(f".{name}.tmp")
    tmp.write_text(f"id: {name}\naccess_tier: owner\ntask: {body}\n")
    tmp.replace(final)


def wait_for(pred, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.2)
    return False


def fswatch_live(p):
    r = subprocess.run(["pgrep", "-P", str(p.pid), "-x", "fswatch"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def wait_for_fswatch(p):
    if not wait_for(lambda: fswatch_live(p), timeout=15):
        raise SystemExit("watcher never started fswatch")


def read_available(p, out):
    try:
        os.set_blocking(p.stdout.fileno(), False)
        c = p.stdout.read()
        if c:
            out.append(c)
    except Exception:
        pass


def snapshot_stderr(errf_path):
    return errf_path.read_text(errors="replace") if errf_path.exists() else ""


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


tmp = Path(tempfile.mkdtemp(prefix="teh-foreign-instance-"))
ws = tmp / "ws"
(ws / "tasks").mkdir(parents=True)
(ws / "results" / "archive").mkdir(parents=True)
(ws / "state").mkdir()
log = tmp / "handler.log"
handler = tmp / "handler.sh"
# A bare "handle" in log would pass trivially once ANY earlier property wrote
# it; record which task file was actually handled, so each property can assert its own.
handler.write_text(
    '#!/bin/sh\n'
    'task=""; prev=""\n'
    'for a in "$@"; do\n'
    '  [ "$prev" = "--task-file" ] && task="$a"\n'
    '  [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }\n'
    '  prev="$a"\n'
    'done\n'
    'echo "handle $(basename "$task")" >> %s\nexit 0\n' % (log, log))
handler.chmod(0o755)

cfg = ws / "state" / "task-event-handler.json"
cfg.write_text(json.dumps({"handler": str(handler)}))

# (1)+(2): the core's own <ws>/tasks, but SUTANDO_INSTANCE_ID carries a
# foreign worker's id.
errf_path = tmp / "watcher.err"
errf = open(errf_path, "w")
p = start_watcher(ws / "tasks", errf, instance="d2571c90f75e4907af9f145b01b71c1f")
out: list[str] = []
try:
    wait_for_fswatch(p)
    write_task(ws / "tasks", "task-one.txt")
    ok = wait_for(lambda: (read_available(p, out), log.exists() and "handle task-one.txt" in log.read_text())[1])
    LAST_STDERR[0] = snapshot_stderr(errf_path)
    check("(1) a foreign inherited SUTANDO_INSTANCE_ID on the core's own inbox is "
          "corrected: the already-present handler still runs",
          ok and log.exists() and "handle task-one.txt" in log.read_text(),
          f"out={out} log={log.read_text() if log.exists() else None}")
    check("(2) the correction is logged to stderr",
          "set while serving the core's own canonical inbox" in LAST_STDERR[0],
          f"stderr={LAST_STDERR[0]!r}")
finally:
    stop(p)
    errf.close()

# (3) a REAL worker-shaped SUTANDO_INBOX_RESOLVER left set rejects every plain
# task body, so the fix must clear the whole routing block, not just the id.
real_resolver = REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry"
errf3_path = tmp / "watcher-full-env.err"
errf3 = open(errf3_path, "w")
p3 = start_watcher(
    ws / "tasks", errf3, instance="d2571c90f75e4907af9f145b01b71c1f",
    extra_env={
        "SUTANDO_INBOX_KIND": "deliveries",
        "SUTANDO_INBOX_RESOLVER": str(real_resolver),
        "SUTANDO_INBOX_RESOLVER_TIMEOUT": "5",
        "SUTANDO_POOL_DELIVERY_SCRIPT": str(REPO / "skills" / "worker-pool" / "scripts" / "pool_delivery.py"),
    })
out3: list[str] = []
try:
    wait_for_fswatch(p3)
    write_task(ws / "tasks", "task-full-env.txt")
    ok3 = wait_for(lambda: (read_available(p3, out3), log.exists() and "handle task-full-env.txt" in log.read_text())[1])
    LAST_STDERR[0] = snapshot_stderr(errf3_path)
    check("(3) a FULL inherited worker env (instance id + inbox kind/resolver/delivery "
          "script) on the core's own inbox is cleared, not just the instance id: "
          "the handler still runs, naming THIS property's own task "
          "(not property (1)'s stale log line)",
          ok3 and log.exists() and "handle task-full-env.txt" in log.read_text(),
          f"out={out3} log={log.read_text() if log.exists() else None} stderr={LAST_STDERR[0]!r}")
finally:
    stop(p3)
    errf3.close()

# (4) control: a worker inbox whose basename is "tasks" (would false-match a
# basename-only check) must keep its identity.
ws2 = tmp / "ws-named-tasks"
worker_inbox_named_tasks = ws2 / "deliveries" / "tasks"
worker_inbox_named_tasks.mkdir(parents=True)
(ws2 / "results" / "archive").mkdir(parents=True)
(ws2 / "state").mkdir()
log2 = tmp / "handler2.log"
handler2 = tmp / "handler2.sh"
handler2.write_text('#!/bin/sh\necho handle >> %s\nexit 0\n' % log2)
handler2.chmod(0o755)
cfg2 = ws2 / "state" / "task-event-handler.json"
cfg2.write_text(json.dumps({"handler": str(handler2)}))

errf4_path = tmp / "watcher-named-tasks.err"
errf4 = open(errf4_path, "w")
p4 = start_watcher(worker_inbox_named_tasks, errf4,
                    instance="d2571c90f75e4907af9f145b01b71c1f", workspace=ws2)
out4: list[str] = []
try:
    wait_for_fswatch(p4)
    write_task(worker_inbox_named_tasks, "task-worker.txt")
    ok4 = wait_for(lambda: (read_available(p4, out4), any("TASK_FILE" in s for s in out4))[1])
    LAST_STDERR[0] = snapshot_stderr(errf4_path)
    check("(4) control: a worker inbox whose basename is 'tasks' (not <ws>/tasks by "
          "realpath) keeps its identity -- the present handler config is NOT read "
          "(a worker never consults it; same rule as "
          "watch-tasks-stream-config-hot-reload.test.py property 3)",
          ok4 and not (log2.exists() and "handle" in log2.read_text()),
          f"out={out4} log2={log2.read_text() if log2.exists() else None} stderr={LAST_STDERR[0]!r}")
    check("(4b) no foreign-identity correction is logged for the real worker inbox",
          "set while serving the core's own canonical inbox" not in LAST_STDERR[0],
          f"stderr={LAST_STDERR[0]!r}")
finally:
    stop(p4)
    errf4.close()

print(f"watch-tasks-stream-foreign-instance-id-corrected: {5 - len(FAILURES)}/5 passed")
for f in FAILURES:
    print(f"  FAILED: {f}")
sys.exit(1 if FAILURES else 0)
