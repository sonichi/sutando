#!/usr/bin/env python3
"""No automated pane writer types while another writer owns the pane.

The pane lock only protects a multi-key transaction if EVERY writer takes it, so a
bypass is invisible to any test that drives one writer alone. Each case here fires
the REAL in-repo writer — never a command shaped like it — at a fake tmux that
records every key, while an independent owner holds the lock the way
`switch-model.sh` holds it across the Codex picker transaction.

Writers covered: `src/health-check.py` (cron nudge), `src/core-input-watch.py`
(auto-answer keypress), `src/agent/codex/cli/task-notifier.sh` (task delivery).

Run: python3 tests/pane-lock-collision.test.py
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSION = "sutando-core"
SOCK = "/tmp/sutando-pane-lock-collision.sock"

sys.path.insert(0, str(REPO / "src"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_tmux(td: Path) -> tuple[Path, Path]:
    """A tmux that says the session is alive and logs everything sent to the pane."""
    log = td / "tmux.log"
    log.unlink(missing_ok=True)
    script = td / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _lock_path() -> str:
    r = subprocess.run(["bash", str(REPO / "scripts" / "tmux-pane-lock.sh"), SOCK, SESSION],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip(), f"lock path not derived: {r.stderr}"
    return r.stdout.strip()


class PaneOwner:
    """Hold the pane lock independently of the helper under test."""

    def __enter__(self):
        self.fd = os.open(_lock_path(), os.O_CREAT | os.O_WRONLY, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *exc):
        self.release()


def _keys(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if "send-keys" in ln]


def case_health_check_cron_nudge() -> list[str]:
    """The writer keweichen demonstrated: `/schedule-crons` + Enter into the pane."""
    fails = []
    hc = _load("health_check_collision", "src/health-check.py")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tmux, log = _fake_tmux(tdp)
        with PaneOwner() as owner:
            got = hc._default_cron_nudge(tmux_bin=str(tmux), sock=SOCK, session=SESSION)
            if got:
                fails.append("health-check: nudge reported success while the pane was owned")
            if _keys(log):
                fails.append(f"health-check: typed into an owned pane: {_keys(log)}")
            owner.release()
            # The control: the same call on a free pane must still type, or the case
            # above would pass for a writer that never works at all.
            if not hc._default_cron_nudge(tmux_bin=str(tmux), sock=SOCK, session=SESSION):
                fails.append("health-check: nudge failed on a FREE pane (control)")
            sent = _keys(log)
            if len(sent) != 1 or "/schedule-crons" not in sent[0]:
                fails.append(f"health-check: expected one /schedule-crons send on a free pane, got {sent}")
    return fails


def case_core_input_watch_keypress() -> list[str]:
    """One auto-answer key is still a write: a digit typed into another writer's
    picker selects a row nobody asked for."""
    fails = []
    ciw = _load("core_input_watch_collision", "src/core-input-watch.py")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tmux, log = _fake_tmux(tdp)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = f"{tdp}{os.pathsep}{env_path}"
        try:
            with PaneOwner() as owner:
                if ciw.send_keys(SOCK, SESSION, "2"):
                    fails.append("core-input-watch: reported a key sent into an owned pane")
                if _keys(log):
                    fails.append(f"core-input-watch: typed into an owned pane: {_keys(log)}")
                owner.release()
                if not ciw.send_keys(SOCK, SESSION, "2"):
                    fails.append("core-input-watch: key failed on a FREE pane (control)")
                if len(_keys(log)) != 1:
                    fails.append(f"core-input-watch: expected one send on a free pane, got {_keys(log)}")
        finally:
            os.environ["PATH"] = env_path
    return fails


def _notifier_repo(td: Path) -> Path:
    """A fixture repo holding only what task-notifier.sh loads at startup."""
    root = td / "repo"
    for rel in (
        "src/agent/codex/cli/task-notifier.sh",
        "src/sutando_config.py",
        "src/workspace_default.py",
        "src/util_paths.py",
        "src/runtime-api/instance_key.py",
        "src/runtime-api/rundir.py",
        "scripts/sutando-config.sh",
        "scripts/python-binary.sh",
        "scripts/tmux-pane-lock.sh",
        "scripts/tmux-pane-lock.bash",
        # tmux-pane-lock.bash delegates the acquisition here; without it every
        # take fails and the notifier looks like it declined rather than could not.
        "src/tmux_pane_lock.py",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    (root / "workspace" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "results").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "tasks" / "task-collision.txt").write_text("task: hello\n")
    return root


def case_task_notifier_delivery() -> list[str]:
    """The delivery loop pastes a prompt then presses C-m; a key landing between
    them is typed into the composer or drives another writer's picker."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        root = _notifier_repo(tdp)
        tmux, log = _fake_tmux(tdp)
        env = dict(os.environ)
        env["PATH"] = f"{tdp}{os.pathsep}{env['PATH']}"
        # Without the sanctioned test hatch the fixture resolves the HOST workspace,
        # so the run would read (and stamp state into) the live one.
        env["SUTANDO_TEST_MODE"] = "1"
        env["SUTANDO_WORKSPACE"] = str(root / "workspace")
        env["SUTANDO_TMUX_SOCKET"] = SOCK
        env["SUTANDO_TMUX_SESSION"] = SESSION
        env["SUTANDO_NOTIFIER_POLL_INTERVAL"] = "0.1"
        env["SUTANDO_NOTIFIER_SUBMIT_RETRIES"] = "1"
        env["SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT"] = "1"
        cmd = ["bash", str(root / "src/agent/codex/cli/task-notifier.sh"),
               "--event", "task-collision.txt"]
        with PaneOwner() as owner:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            # It must WAIT for the pane, not type into it: a delivery is not droppable.
            deadline = time.time() + 6
            while time.time() < deadline and proc.poll() is None:
                if _keys(log):
                    fails.append(f"task-notifier: typed into an owned pane: {_keys(log)}")
                    break
                time.sleep(0.2)
            if proc.poll() is not None and not _keys(log):
                fails.append("task-notifier: exited without delivering instead of waiting for the pane")
            owner.release()
            try:
                proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                proc.kill()
                fails.append("task-notifier: still running well after the pane was released")
        sent = _keys(log)
        if not any("task-collision.txt" in ln for ln in sent):
            fails.append(f"task-notifier: never delivered after the pane was released (control), log: {sent}")
    return fails


CASES = (
    ("health-check cron nudge", case_health_check_cron_nudge),
    ("core-input-watch keypress", case_core_input_watch_keypress),
    ("task-notifier delivery", case_task_notifier_delivery),
)


def main() -> int:
    failures = []
    for name, fn in CASES:
        try:
            got = fn()
        except Exception as e:  # a case that cannot run is a failure, never a pass
            got = [f"{name}: raised {e!r}"]
        if got:
            failures.extend(got)
            print(f"  ✗ {name}")
            for f in got:
                print(f"      {f}")
        else:
            print(f"  ✓ {name}")
    if failures:
        print(f"\npane-lock collision: {len(failures)} FAILED")
        return 1
    print("\nEvery automated pane writer defers to the pane's owner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
