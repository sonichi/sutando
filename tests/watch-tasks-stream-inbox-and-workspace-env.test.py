#!/usr/bin/env python3
"""The watcher's inbox and its workspace may each be named by env.

Two generic seams any multi-instance install needs, pinned from both sides with
no handler and no pool module present:

  SUTANDO_TASKS_DIR    — watch this folder, and do NOT create <ws>/tasks/.
  SUTANDO_WORKSPACE_DIR — put derived state (claims, fallbacks) HERE, rather
                          than inferring it from the watched inbox's parent.

The second is only meaningful because of the first: once the inbox is
<ws>/deliveries/<id>, its parent is deliveries/, not the workspace.

Run: python3 tests/watch-tasks-stream-inbox-and-workspace-env.test.py
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


def _private_repo(td: Path) -> Path:
    """A private copy of src/ + scripts/ whose sutando-config.sh is a stub, so
    the watcher under test can never resolve the caller's live workspace."""
    root = td / "repo"
    if not root.exists():
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
    ws = td / "ws"
    ws.mkdir(exist_ok=True)
    (root / "scripts" / "sutando-config.sh").write_text(
        '#!/bin/bash\ncase "$1" in workspace) echo "%s";; python-bin) echo %s;; *) echo "";; esac\n'
        % (ws, sys.executable)
    )
    return root


def _run_watcher(root: Path, env: dict, ready, timeout: float = 8.0) -> None:
    """Run the watcher until `ready()` or the deadline. It is a process GROUP
    (bash, fswatch, a sleep loop); killing the leader alone leaves children."""
    p = subprocess.Popen(
        ["bash", str(root / "src" / "watch-tasks-stream.sh")],
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


def _base_env(**extra) -> dict:
    env = {**os.environ, **extra}
    for k in ("SUTANDO_TASKS_DIR", "SUTANDO_WORKSPACE_DIR"):
        if k not in extra:
            env.pop(k, None)
    return env


def _watched_dir(extra_env: dict, td: Path) -> tuple[bool, bool]:
    """Report which folder the watcher created: (the workspace's tasks/, the override)."""
    root = _private_repo(td)
    ws = td / "ws"
    env = _base_env(SUTANDO_RESULTS_DIR=str(ws / "results"), **extra_env)
    _run_watcher(root, env, lambda: (ws / "tasks").is_dir() or (td / "deliveries").is_dir())
    return (ws / "tasks").is_dir(), (td / "deliveries").is_dir()


def _state_root(extra_env: dict, td: Path) -> tuple[bool, bool, bool]:
    """Run a watcher on a delivery folder with a stub handler set, so it creates
    its claims dir at boot; report where that dir landed, and whether the named
    inbox was honoured at all (else both answers are about <ws>/tasks/)."""
    root = _private_repo(td)
    ws = td / "ws"
    handler = td / "handler.sh"
    handler.write_text("#!/bin/bash\nexit 3\n")
    handler.chmod(0o755)
    inbox = td / "deliveries" / ("b" * 32)
    env = _base_env(
        SUTANDO_RESULTS_DIR=str(ws / "results"),
        SUTANDO_TASKS_DIR=str(inbox),
        SUTANDO_TASK_EVENT_HANDLER=str(handler),
        **extra_env,
    )
    under_ws = ws / "state" / "task-event-handler-claims"
    under_inbox = td / "deliveries" / "state" / "task-event-handler-claims"
    _run_watcher(root, env, lambda: under_ws.is_dir() or under_inbox.is_dir())
    return under_ws.is_dir(), under_inbox.is_dir(), inbox.is_dir()


class TestInboxEnvSeam(unittest.TestCase):
    def test_unset_the_watcher_watches_its_workspace_tasks_folder(self):
        """Control: absent the variable, the resolved workspace still decides."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tasks, override = _watched_dir({}, Path(td))
        self.assertTrue(tasks, "the workspace's tasks/ was not the watched folder")
        self.assertFalse(override)

    def test_set_the_named_inbox_is_watched_and_tasks_is_not_created(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tasks, override = _watched_dir(
                {"SUTANDO_TASKS_DIR": str(Path(td) / "deliveries")}, Path(td)
            )
        self.assertTrue(override, "the named inbox was not the watched folder")
        self.assertFalse(tasks, "an instance with its own inbox must not create <ws>/tasks/")


class TestWorkspaceEnvSeam(unittest.TestCase):
    def test_an_explicit_workspace_keeps_derived_state_out_of_the_inbox_tree(self):
        """Whoever names the inbox names the workspace; the watcher must not
        infer it from <ws>/deliveries/<id>."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ws_hit, inbox_hit, watched = _state_root(
                {"SUTANDO_WORKSPACE_DIR": str(Path(td) / "ws")}, Path(td)
            )
        self.assertTrue(watched, "the named inbox was never watched — the rest is vacuous")
        self.assertTrue(ws_hit, "claims dir was not created under the explicit workspace")
        self.assertFalse(inbox_hit, "claims dir leaked under deliveries/")

    def test_without_it_the_inbox_parent_is_taken_as_the_workspace(self):
        """Control: the seam exists — absent the variable, state lands under deliveries/."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ws_hit, inbox_hit, watched = _state_root({}, Path(td))
        self.assertTrue(watched, "the named inbox was never watched — the rest is vacuous")
        self.assertTrue(inbox_hit, "the inbox's parent was not taken as the workspace")
        self.assertFalse(ws_hit)


if __name__ == "__main__":
    unittest.main(verbosity=0)
