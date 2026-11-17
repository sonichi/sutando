#!/usr/bin/env python3
"""The standby supervisor's mode decision, with no tmux: alert mode never starts the typing notifier.

A stub `tmux` on PATH answers has-session from a flag file, so the real supervisor runs its top-level
branch on any runner. With SUTANDO_STANDBY_MODE=alert it alerts and never execs the notifier; with
the mode unset, the same harness sees the notifier start (the control that the harness can see it).
Run: python3 tests/task-notifier-supervisor-alert-decision.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ()

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.process_group import kill_group, popen_in_own_group

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"

TMUX_STUB = """#!/bin/bash
for a in "$@"; do
  if [ "$a" = has-session ]; then [ -e "$SUT_TEST_ALIVE" ]; exit $?; fi
done
exit 1
"""
NOTIFIER_STUB = '#!/bin/bash\necho "$$ $*" >> "$SUT_TEST_MARKER"\nexit 0\n'
OSASCRIPT_STUB = '#!/bin/bash\necho "$*" >> "$SUT_TEST_OSA"\n'


def _wait(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


class AlertDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        for d in ("tasks", "state", "logs", "results"):
            (self.ws / d).mkdir(parents=True)
        stubs = root / "bin"
        stubs.mkdir()
        for name, body in (("tmux", TMUX_STUB), ("osascript", OSASCRIPT_STUB)):
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        self.notifier = root / "notifier.sh"
        self.notifier.write_text(NOTIFIER_STUB)
        self.notifier.chmod(0o755)
        self.alive = root / "alive"
        self.marker = root / "notifier.marker"
        self.env = hermetic_env(
            PATH=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            SUT_TEST_ALIVE=str(self.alive), SUT_TEST_MARKER=str(self.marker),
            SUT_TEST_OSA=str(root / "osa.log"),
            SUTANDO_TMUX_SOCKET=str(root / "none.sock"), SUTANDO_TMUX_SESSION="decision-target",
            SUTANDO_TASKS_DIR=str(self.ws / "tasks"), SUTANDO_NOTIFIER_SCRIPT=str(self.notifier),
            SUTANDO_NOTIFIER_GRACE_PERIOD="0", SUTANDO_NOTIFIER_ROLE_POLL="1",
            SUTANDO_NOTIFIER_TARGET_POLL="1", SUTANDO_NOTIFIER_RESTART_DELAY="1",
            SUTANDO_NOTIFIER_ALERT_GRACE="1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, mode: "str | None", until) -> None:
        env = dict(self.env)
        if mode is not None:
            env["SUTANDO_STANDBY_MODE"] = mode
        self.alive.write_text("")
        proc = popen_in_own_group(["bash", str(SUPERVISOR)], env=env, cwd=str(REPO))
        try:
            _wait(until, 20)
            self.alive.unlink()
            proc.wait(timeout=15)
        finally:
            kill_group(proc.pid)

    def _alerts(self) -> list:
        return sorted((self.ws / "results").glob("proactive-standby-unwatched-*.txt"))

    def test_alert_mode_alerts_and_never_starts_the_typing_notifier(self):
        self._run("alert", lambda: bool(self._alerts()))
        self.assertTrue(self._alerts(), "the alert branch never alerted")
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

    def test_without_the_mode_the_same_harness_sees_the_notifier_start(self):
        self._run(None, self.marker.exists)
        self.assertTrue(self.marker.exists(), "control: the typing notifier did not start")
        self.assertEqual(self._alerts(), [])


if __name__ == "__main__":
    unittest.main()
