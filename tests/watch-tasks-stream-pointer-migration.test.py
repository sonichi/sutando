#!/usr/bin/env python3
"""The watcher runs the archived-pointer migration once per workspace, before its
first sweep plan, and records that in a marker so a later start never repeats it.
A migration that fails writes no marker, so the next start runs it again.

Run: python3 tests/watch-tasks-stream-pointer-migration.test.py
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

W = "0123456789abcdef0123456789abcdef"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def run_watcher(tmp: Path, ws: Path, inbox: Path, until, extra_env=None) -> str:
    feed = tmp / "feed"
    feed.write_text("")
    (tmp / "bin").mkdir(exist_ok=True)
    stub = tmp / "bin" / "fswatch"
    stub.write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    stub.chmod(0o755)
    env = clean_env()
    env.update(PATH=f"{tmp / 'bin'}:{env['PATH']}", TMPDIR=str(tmp), SUTANDO_INSTANCE_ID=W, SUTANDO_INSTANCE=W,
               SUTANDO_WORKSPACE_DIR=str(ws), SUTANDO_RESULTS_DIR=str(ws / "results"),
               SUTANDO_INBOX_RESOLVER=str(REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry"))
    env.update(extra_env or {})
    err = tmp / "err"
    with err.open("w") as fh:
        p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", "standby", "--inbox", str(inbox)],
                             cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=fh, start_new_session=True)
        wait_for(lambda: until(err.read_text()))
        time.sleep(0.5)
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)
    return err.read_text()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ptr-migration-")).resolve()
    ws = tmp / "ws"
    inbox = ws / "deliveries" / W
    for d in (ws / "tasks" / "archive", ws / "results", ws / "state", inbox):
        d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (ws / "tasks" / "archive" / f"task-old{i}.txt").write_text("id: x\n")
        (inbox / f"task-old{i}.txt").write_text("")
    (ws / "tasks" / "task-live.txt").write_text("id: task-live\ntask: x\n")
    (inbox / "task-live.txt").write_text("")
    marker = ws / "state" / "migrations" / "retire-archived-pointers.v1.done"
    # A python that fails the migration subcommand only; everything else runs for real.
    failing_py = tmp / "bin" / "failing-python3"
    (tmp / "bin").mkdir(exist_ok=True)
    failing_py.write_text('#!/bin/sh\ncase "$2" in retire-archived-pointers) echo "boom" >&2; exit 1;; esac\n'
                          f'exec "{sys.executable}" "$@"\n')
    failing_py.chmod(0o755)
    try:
        err = run_watcher(tmp, ws, inbox, lambda e: "sweep plan over" in e, {"SUTANDO_PY": str(failing_py)})
        check("a failed migration is reported and retried next start",
              "pointer migration failed; it retries on the next start: boom" in err, err[-500:])
        check("...is not recorded as done", not marker.exists() and not list(marker.parent.glob("*")))
        check("...leaves every pointer in place", all((inbox / f"task-old{i}.txt").exists() for i in range(3)))
        check("...and the sweep still planned the inbox", "sweep plan over 4 entries" in err, err[-500:])
        err = run_watcher(tmp, ws, inbox, lambda e: "sweep plan over" in e)
        check("the next start migrated the archived pointers", 'pointer migration: {"retired": 3' in err, err[-500:])
        check("...before the sweep planned the inbox", "sweep plan over 1 entries" in err, err[-500:])
        check("...and left the pending pointer in place", (inbox / "task-live.txt").exists())
        check("the marker records the run", marker.is_file() and '"retired": 3' in marker.read_text())
        (ws / "tasks" / "archive" / "task-later.txt").write_text("id: x\n")
        (inbox / "task-later.txt").write_text("")
        err = run_watcher(tmp, ws, inbox, lambda e: "sweep plan over" in e)
        check("a later start does not run it again", "pointer migration" not in err, err[-500:])
        check("...so a pointer archived afterwards stays for the archive step", (inbox / "task-later.txt").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nPASS" if not FAILURES else f"\nFAIL — {len(FAILURES)} check(s) failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
