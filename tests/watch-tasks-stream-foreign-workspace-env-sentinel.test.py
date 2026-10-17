#!/usr/bin/env python3
"""A watcher on an inbox OUTSIDE SUTANDO_WORKSPACE_DIR must not stamp that workspace.

A test or tool that copies a live core's env and starts a watcher on a temp inbox
inherits SUTANDO_WORKSPACE_DIR. The watcher must then derive its state dir from its
own inbox, never write the named (live) workspace's readiness sentinel. The named
workspace still holds for an inbox inside it (a worker's <ws>/deliveries/<id>) and
for the inbox SUTANDO_TASKS_DIR names alongside it (the launcher named both).

Run: python3 tests/watch-tasks-stream-foreign-workspace-env-sentinel.test.py
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402

LIVE_PID = "424242\n"


def _private_repo(td: Path, ws: Path) -> Path:
    """src/ + scripts/ copy whose sutando-config.sh names `ws`, never the caller's."""
    root = td / "repo"
    shutil.copytree(REPO / "src", root / "src", symlinks=True)
    shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
    (root / "scripts" / "sutando-config.sh").write_text(
        '#!/bin/bash\ncase "$1" in workspace) echo "%s";; python-bin) echo %s;; *) echo "";; esac\n'
        % (ws, sys.executable)
    )
    return root


def _sentinels(state: Path) -> dict:
    if not state.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in state.iterdir()
            if p.name.startswith("watch-tasks-stream") and p.name.endswith(".pid")}


def _run_watcher(root: Path, env: dict, inbox: Path, ready, timeout: float = 10.0) -> None:
    p = subprocess.Popen(
        ["bash", str(root / "src" / "watch-tasks-stream.sh"),
         "--role", "standby", "--inbox", str(inbox)],
        cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and not ready() and p.poll() is None:
            time.sleep(0.1)
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        time.sleep(0.2)


class ForeignWorkspaceEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.td = Path(tempfile.mkdtemp(prefix="foreign-ws-env-"))
        self.live = self.td / "live"
        (self.live / "state").mkdir(parents=True)
        (self.live / "state" / "watch-tasks-stream.pid").write_text(LIVE_PID)
        self.ws = self.td / "ws"
        self.root = _private_repo(self.td, self.ws)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def _env(self, **extra) -> dict:
        env = clean_env()
        env.update(SUTANDO_WORKSPACE_DIR=str(self.live), **extra)
        return env

    def test_temp_inbox_does_not_touch_the_named_live_workspace(self):
        inbox = self.ws / "tasks"
        inbox.mkdir(parents=True)
        before = _sentinels(self.live / "state")
        own = self.ws / "state"
        _run_watcher(self.root, self._env(), inbox, lambda: bool(_sentinels(own))
                     or _sentinels(self.live / "state") != before)
        self.assertEqual(_sentinels(self.live / "state"), before,
                         "the watcher rewrote the named live workspace's readiness sentinel")
        self.assertTrue(_sentinels(own),
                        "control: the watcher never stamped its own inbox's workspace")

    def test_inbox_inside_the_named_workspace_still_uses_it(self):
        """A worker's <ws>/deliveries/<id> keeps stamping the named workspace."""
        inbox = self.live / "deliveries" / ("c" * 32)
        inbox.mkdir(parents=True)
        (self.live / "state" / "watch-tasks-stream.pid").unlink()
        _run_watcher(self.root, self._env(SUTANDO_TASKS_DIR=str(inbox)), inbox,
                     lambda: bool(_sentinels(self.live / "state")))
        self.assertTrue(_sentinels(self.live / "state"),
                        "an inbox inside the named workspace no longer stamps it")
        self.assertFalse(_sentinels(self.live / "deliveries" / "state"))

    def test_an_inbox_named_with_the_workspace_still_uses_it(self):
        """SUTANDO_TASKS_DIR + SUTANDO_WORKSPACE_DIR name a pair, ancestor or not."""
        inbox = self.td / "custom" / "tasks"
        inbox.mkdir(parents=True)
        (self.live / "state" / "watch-tasks-stream.pid").unlink()
        _run_watcher(self.root, self._env(SUTANDO_TASKS_DIR=str(inbox)), inbox,
                     lambda: bool(_sentinels(self.live / "state")))
        self.assertTrue(_sentinels(self.live / "state"),
                        "an inbox named alongside the workspace no longer stamps it")
        self.assertFalse(_sentinels(self.td / "custom" / "state"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
