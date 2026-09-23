#!/usr/bin/env python3
"""A held task recovers on the config file's own event even when the workspace
is named through a symlink.

fswatch reports PHYSICAL paths. When SUTANDO_WORKSPACE_DIR carries a symlinked
spelling, the config path derived from it must still match the event the
watcher receives for the config file; otherwise the reload-and-redispatch that
event carries never runs, and a task held on a broken config waits for some
later task's event instead. Against a live watcher process and REAL fswatch,
with no poll timer to fall back on:

1. The config exists but is unreadable at start: a task is held (no handler
   call, no TASK_FILE line).
2. A readable config is published (tmp-then-rename, the exact write
   pool_roster.publish_task_event_handler() performs) and NOTHING else
   happens: the held task is dispatched through the handler on that event,
   within a window shorter than any poll interval.

Run: python3 tests/watch-tasks-stream-config-hot-reload-symlinked-workspace.test.py
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


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


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


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


def start_watcher(ws_spelling):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("SUTANDO_"):
            env.pop(k)
    # The symlinked spelling, verbatim: this is what the launcher hands a core.
    env["SUTANDO_WORKSPACE_DIR"] = str(ws_spelling)
    env["SUTANDO_RESULTS_DIR"] = str(ws_spelling / "results")
    # Any timer must not be what makes (2) pass: keep it far beyond the wait.
    env["SUTANDO_HANDLER_POLL_INTERVAL"] = "600"
    env["SUTANDO_HELD_RETRY_INTERVAL"] = "600"
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws_spelling / "tasks"),
         "--role", "standby", "--inbox", str(ws_spelling / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)


def write_task(ws, name, body="probe"):
    final = ws / "tasks" / name
    tmp = final.with_name(f".{name}.tmp")
    tmp.write_text(f"id: {name}\naccess_tier: owner\ntask: {body}\n")
    tmp.replace(final)


def publish(cfg, handler):
    tmp_cfg = cfg.with_name(f".{cfg.name}.tmp")
    tmp_cfg.write_text(json.dumps({"handler": str(handler)}))
    tmp_cfg.replace(cfg)


tmp = Path(tempfile.mkdtemp(prefix="teh-hotreload-symlink-")).resolve()
real_ws = tmp / "real" / "ws"
(real_ws / "tasks").mkdir(parents=True)
(real_ws / "results" / "archive").mkdir(parents=True)
(real_ws / "state").mkdir()
(tmp / "link").symlink_to(tmp / "real")
ws = tmp / "link" / "ws"  # every path below is spelled through the symlink
assert os.path.realpath(ws) == str(real_ws)

log = tmp / "handler.log"
handler = tmp / "handler.sh"
handler.write_text(
    '#!/bin/sh\n'
    'for a in "$@"; do [ "$a" = "--probe" ] && { echo probe >> %s; exit 0; }; done\n'
    'echo handle >> %s\nexit 0\n' % (log, log))
handler.chmod(0o755)
cfg = ws / "state" / "task-event-handler.json"
publish(cfg, handler)
os.chmod(cfg, 0)  # present but unreadable: the watcher holds tasks on it

p = start_watcher(ws)
out: list[str] = []
try:
    wait_for_fswatch(p)
    time.sleep(1.0)
    write_task(ws, "task-one.txt")
    time.sleep(3.0)
    read_available(p, out)
    check("(1) unreadable config: the task is held (no handler call, no TASK_FILE line)",
          not log.exists() and not any("TASK_FILE" in s for s in out),
          f"out={out} log={log.read_text() if log.exists() else None}")

    os.chmod(cfg, 0o644)
    publish(cfg, handler)  # the config's own event, and nothing else
    out2: list[str] = []
    ok2 = wait_for(lambda: (read_available(p, out2), log.exists() and "handle" in log.read_text())[1])
    check("(2) readable config published: the held task is dispatched through it on that event alone",
          ok2 and log.exists() and "handle" in log.read_text() and not any("TASK_FILE" in s for s in out2),
          f"out2={out2} log={log.read_text() if log.exists() else None}")
finally:
    try:
        os.chmod(cfg, 0o644)
    except OSError:
        pass
    stop(p)

if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("\nall checks passed")
