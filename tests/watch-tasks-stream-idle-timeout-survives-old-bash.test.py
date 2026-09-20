#!/usr/bin/env python3
"""On macOS, /bin/bash is a frozen 3.2.57 (GPLv2) build, and `read -t` returns
exit status 1 there for BOTH a real timeout and real EOF -- unlike a modern
bash, which reliably returns >128 on timeout. Both launchers (Claude and
Codex) explicitly run the watcher through /bin/bash, never a PATH-resolved
one, so this is the real deployed behavior, not a hypothetical.

Before the fix: the read loop distinguished timeout from EOF purely by exit
code (`-gt 128` = timeout, else = EOF), which is wrong on /bin/bash 3.2 -- a
quiet FIFO for one poll interval reads as EOF, the loop breaks, and the
script exits its normal cleanup path, killing fswatch with it. A task that
arrives after that silent death is never delivered.

After the fix: a nonzero read is checked against fswatch's actual liveness
(kill -0 $FSWATCH_PID) before deciding EOF -- same pattern already used by
src/agent/codex/cli/task-notifier.sh. Reviewed by keweichen on PR #4503.

This test explicitly invokes the script via /bin/bash (never a bare "bash",
which on this host resolves to Homebrew's modern 5.x and would never
reproduce the bug) with a short poll interval, an idle period spanning
several poll intervals, and a task delivered only after that idle period.

Run: python3 tests/watch-tasks-stream-idle-timeout-survives-old-bash.test.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
SYSTEM_BASH = "/bin/bash"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout: float = 15.0, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


class Harness:
    def __init__(self) -> None:
        if not Path(SYSTEM_BASH).exists():
            raise RuntimeError(f"{SYSTEM_BASH} not present on this host -- cannot reproduce")
        self.tmp = Path(tempfile.mkdtemp(prefix="idle-timeout-test-"))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results" / "archive").mkdir(parents=True)
        (self.ws / "state").mkdir()
        self.feed = self.tmp / "feed"
        self.feed.write_text("")
        stub_dir = self.tmp / "bin"
        stub_dir.mkdir()
        (stub_dir / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {self.feed}\n")
        (stub_dir / "fswatch").chmod(0o755)
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []

    def task(self, name: str) -> Path:
        p = self.ws / "tasks" / name
        p.write_text(f"id: {name[:-4]}\naccess_tier: team\ntask: probe\n")
        return p

    def start(self) -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp/'bin'}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["SUTANDO_RESULTS_DIR"] = str(self.ws / "results")
        env["SUTANDO_HANDLER_POLL_INTERVAL"] = "1"   # short, to hit the timeout branch fast
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        env.pop("SUTANDO_INSTANCE_ID", None)
        self.proc = subprocess.Popen(
            [SYSTEM_BASH, "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def deliver_file_event(self, path: Path) -> None:
        with self.feed.open("a") as fh:
            fh.write(str(path.resolve()) + "\n")

    def drain_stdout(self) -> None:
        try:
            os.set_blocking(self.proc.stdout.fileno(), False)
            c = self.proc.stdout.read()
            if c:
                self.lines.extend(c.splitlines())
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def dispatch_count(self, name: str) -> int:
        self.drain_stdout()
        return sum(1 for l in self.lines if l.strip() == f"TASK_FILE: {name}")

    def cleanup(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass


def test_watcher_survives_idle_period_under_system_bash():
    h = Harness()
    try:
        h.start()
        ok = wait_for(lambda: h.alive(), timeout=5)
        check("watcher starts under /bin/bash", ok)

        # Idle for several poll intervals -- /bin/bash 3.2's read -t returns
        # exit 1 for this, indistinguishable by code alone from real EOF.
        time.sleep(4.0)
        check("watcher is STILL ALIVE after an idle period (not misread as EOF)",
              h.alive(), "process exited -- idle timeout was misread as EOF")

        # Deliver only now, after the idle window -- alive alone isn't
        # enough, prove it still dispatches.
        p = h.task("task-after-idle.txt")
        h.deliver_file_event(p)
        ok = wait_for(lambda: h.dispatch_count("task-after-idle.txt") >= 1, timeout=10)
        check("a task delivered after the idle period is still dispatched", ok,
              f"lines: {h.lines}")
    finally:
        h.cleanup()


def main() -> int:
    test_watcher_survives_idle_period_under_system_bash()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
