#!/usr/bin/env python3
"""The Codex notifier never types a worker-held or watcher-claimed task into
the core: filename_is_worker_held/filename_is_claimed are extracted from
src/agent/codex/cli/task-notifier.sh and run directly, so the assertion is on
the shipped functions, not a copy.

These replace the old test against next_pending_task/probe_optional_task_handler,
deleted by the single-decider redesign -- the notifier no longer picks or
probes; it only validates an announced filename before typing it.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found in {SCRIPT}"
    return m.group(0)


class CodexNotifierGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name)
        (ws / "state" / "task-event-handler-claims").mkdir(parents=True)
        (ws / "deliveries" / "worker-1").mkdir(parents=True)
        (ws / "deliveries" / "worker-1" / "task-held.claimed").write_text("")
        (ws / "state" / "task-event-handler-claims" / "task-claimed.txt").write_text(
            "12345\n12345-1\n/tmp/task-claimed.txt\nfallback\n")
        self.ws = ws

    def _check(self, fn_name: str, filename: str) -> int:
        text = SCRIPT.read_text()
        fn = _function_text(fn_name, text)
        harness = fn + f'\n{fn_name} "{filename}"\n'
        env = {**os.environ,
               "DELIVERIES_DIR": str(self.ws / "deliveries"),
               "CLAIMS_DIR": str(self.ws / "state" / "task-event-handler-claims"),
               "NOTIFIER_PY": sys.executable,
               "DISPATCH_PY": str(REPO / "src" / "delivery" / "task_dispatch.py")}
        r = subprocess.run(["bash", "-c", harness], env=env, timeout=30)
        return r.returncode

    def test_shipped_worker_held_check_blocks_the_held_task(self):
        self.assertEqual(self._check("filename_is_worker_held", "task-held.txt"), 0)

    def test_shipped_worker_held_check_allows_a_free_task(self):
        self.assertEqual(self._check("filename_is_worker_held", "task-free.txt"), 1)

    def test_shipped_claimed_check_blocks_the_claimed_task(self):
        self.assertEqual(self._check("filename_is_claimed", "task-claimed.txt"), 0)

    def test_shipped_claimed_check_allows_an_unclaimed_task(self):
        self.assertEqual(self._check("filename_is_claimed", "task-free.txt"), 1)


if __name__ == "__main__":
    unittest.main()
