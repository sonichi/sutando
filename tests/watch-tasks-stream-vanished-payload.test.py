#!/usr/bin/env python3
"""A must-handle task whose payload is moved away mid-run publishes no terminal failure.

After a usage-limit stop, /startup's task-orphan-check archived the inbox while the
standby watcher was still running the router on those tasks; each handler failed on
the vanished file and the watcher posted "could not safely process ... restricted
runtime failed" into rooms (sonichi/sutando#5673). The archive owns those tasks now.

Control: the same rc-4 failure with the payload still in place still publishes the
terminal failure, so the fix narrows the failure path rather than removing it.

Run: python3 tests/watch-tasks-stream-vanished-payload.test.py
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.process_group import popen_bound  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEADLINE_S = 40.0
FAILURES: list[str] = []


def scenario(move_payload: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="vanish-"))
    atexit.register(shutil.rmtree, tmp, True)
    ws = tmp / "ws"
    (ws / "tasks" / "archive").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    feed = tmp / "feed"
    feed.write_text("")
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)
    handler = tmp / "handler.sh"
    move = f'mv "$task" "{ws / "tasks" / "archive"}/"\n' if move_payload else ""
    # Probe says must-handle (rc 4); the real run optionally loses its payload, then fails rc 4.
    handler.write_text('#!/bin/sh\ntask=""\nprev=""\n'
                       'for a in "$@"; do [ "$a" = "--probe" ] && exit 4\n'
                       '  [ "$prev" = "--task-file" ] && task="$a"; prev="$a"; done\n'
                       f'{move}exit 4\n')
    handler.chmod(0o755)
    hook = tmp / "hook.sh"
    hook.write_text('#!/bin/sh\nprintf "%s %s\\n" "$1" "$2" >> "$TRANSITIONS"\n')
    hook.chmod(0o755)
    transitions = tmp / "transitions.log"
    (ws / "tasks" / "task-demo.txt").write_text("id: task-demo\naccess_tier: team\ntask: probe\n")
    env = clean_env()
    env.update({"PATH": f"{b}:{env['PATH']}", "TMPDIR": str(tmp),
                "SUTANDO_RESULTS_DIR": str(ws / "results"),
                "SUTANDO_TASK_EVENT_HANDLER": str(handler),
                "SUTANDO_WATCHER_TRANSITION_HOOK": str(hook),
                "TRANSITIONS": str(transitions)})
    errf = open(tmp / "watcher.err", "w+")
    p = popen_bound(["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
                     "--role", "standby", "--inbox", str(ws / "tasks")],
                    cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=errf)
    os.set_blocking(p.stdout.fileno(), False)
    claim = ws / "state" / "task-event-handler-claims" / "task-demo.txt"
    out = b""

    def settled() -> bool:
        seen = transitions.read_text() if transitions.exists() else ""
        return "after-handler task-demo.txt" in seen and not claim.exists()

    end = time.time() + DEADLINE_S
    while time.time() < end and p.poll() is None and not settled():
        try:
            out += os.read(p.stdout.fileno(), 65536)
        except (BlockingIOError, OSError):
            pass
        time.sleep(0.1)
    ok = settled()
    time.sleep(1.0)
    try:
        out += os.read(p.stdout.fileno(), 65536)
    except (BlockingIOError, OSError):
        pass
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    errf.seek(0)
    return {"settled": ok, "emitted": b"TASK_FILE" in out,
            "results": sorted(q.name for q in (ws / "results").glob("*.txt")),
            "archived": (ws / "tasks" / "archive" / "task-demo.txt").exists(),
            "stderr": errf.read()[-1500:]}


def check(name: str, cond: bool, r: dict) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILURES.append(name)
        print(f"    {r}")


vanished = scenario(move_payload=True)
check("vanished: the handler ran and the claim was released", vanished["settled"], vanished)
check("vanished: the payload really was moved away mid-run", vanished["archived"], vanished)
check("vanished: no terminal-failure result is published", vanished["results"] == [], vanished)
check("vanished: nothing is handed to the live core", not vanished["emitted"], vanished)

present = scenario(move_payload=False)
check("control: the claim settles with the payload in place", present["settled"], present)
check("control: a genuine must-handle failure still publishes its terminal failure",
      present["results"] == ["task-demo.txt"], present)
check("control: nothing is handed to the live core", not present["emitted"], present)

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else "PASS — a vanished payload publishes no terminal failure")
sys.exit(1 if FAILURES else 0)
