"""A session watcher dispatches a task admitted after readiness with the handler
config fswatch delivered before that task's event, the same guarantee the live
loop gives.

The readiness probe buffers every event fswatch delivers before the probe comes
back. A handler config and a task that both land in that window must be
handled in fswatch's order: config first means the task goes through the
handler (a must-handle task is never announced to the live core); task first
means the task may reach the core, as it would on the live loop, and the
config applies to the next task.

Run: python3 tests/watch-tasks-stream-readiness-window-honours-handler-config.test.py
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


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def scenario(config_first: bool):
    """Start a session watcher whose fswatch delivers events only after a delay
    (a wide readiness window). config_first: publish a must-handle config and a
    Team task inside the window. Otherwise: the task inside the window, the
    config only after the task was announced, then a second task.
    Returns (stdout lines, handler log, result names)."""
    tmp = Path(tempfile.mkdtemp(prefix="ready-window-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    # Subscribes at once, delivers 2 s late: the probe, the config and the task
    # all stay buffered, so the window is wide and the test controls the order.
    (b / "fswatch").write_text(
        "#!/bin/bash\nbash "
        f"{REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\" | {{ sleep 2; exec cat; }}\n")
    (b / "fswatch").chmod(0o755)
    log = tmp / "handler.log"
    handler = tmp / "handler.sh"
    handler.write_text(
        '#!/bin/sh\n'
        f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe >> {log}; exit 4; }}; done\n'
        f'echo handle >> {log}\nexit 4\n')
    handler.chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"

    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    p = subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    out: list[str] = []
    try:
        # The probe is written first and fswatch's 2 s delay holds it back, so the
        # probe, the config and the task are all buffered when readiness returns.
        time.sleep(0.6)

        def publish_config():
            t = cfg.with_name(".cfg.tmp")
            t.write_text(json.dumps({"handler": str(handler)}))
            t.replace(cfg)

        def write_task(name):
            final = ws / "tasks" / f"{name}.txt"
            t = final.with_name(f".{name}.tmp")
            t.write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
            t.replace(final)

        os.set_blocking(p.stdout.fileno(), False)

        def pump(until, timeout=12):
            t0 = time.time()
            while time.time() - t0 < timeout:
                time.sleep(0.3)
                try:
                    c = p.stdout.read()
                except (BlockingIOError, TypeError):
                    c = None
                if c:
                    out.extend(c.splitlines())
                if until():
                    time.sleep(1.0)
                    try:
                        c = p.stdout.read()
                    except (BlockingIOError, TypeError):
                        c = None
                    if c:
                        out.extend(c.splitlines())
                    return

        if config_first:
            publish_config()
            time.sleep(0.3)
            write_task("task-team")
            pump(lambda: log.exists() or any("task-team" in ln for ln in out))
        else:
            write_task("task-team")
            pump(lambda: any("TASK_FILE: task-team" in ln for ln in out))
            publish_config()
            time.sleep(0.5)
            write_task("task-next")
            pump(lambda: log.exists() or any("task-next" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        results = sorted(f.name for f in (ws / "results").glob("task-*.txt"))
        return out, handled, results
    finally:
        try:
            os.killpg(p.pid, 15)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


print("config then task, both inside the readiness window:")
out, handled, results = scenario(config_first=True)
leaked = any(ln.startswith("TASK_FILE: task-team") for ln in out)
check("the Team task is NOT announced to the live core", not leaked,
      f"stdout={out!r}")
check("the Team task went through the handler exactly once (one probe, one run)",
      handled.count("probe") == 1 and handled.count("handle") == 1,
      f"handler log={handled!r} results={results!r}")
check("the handler's must-handle verdict published a terminal result", results != [],
      f"results={results!r}")

print("task inside the window, config only after it was announced, then a second task:")
out, handled, results = scenario(config_first=False)
check("the earlier task reached the core exactly once (no config existed at its decision)",
      sum(ln.startswith("TASK_FILE: task-team") for ln in out) == 1,
      f"stdout={out!r}")
check("the later config applies to the next task: not announced, handled once",
      not any(ln.startswith("TASK_FILE: task-next") for ln in out)
      and handled.count("probe") == 1 and handled.count("handle") == 1,
      f"stdout={out!r} handler log={handled!r} results={results!r}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else
      "PASS — a task admitted after readiness sees the handler config fswatch delivered before it")
sys.exit(1 if FAILURES else 0)
