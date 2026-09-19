#!/usr/bin/env python3
"""The Codex notifier's pick never selects a worker-held task.

`next_pending_task` is extracted from src/agent/codex/cli/task-notifier.sh and run
in a bash harness, so the assertion is on the shipped function, not a copy.
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


class CodexNotifierWorkerHoldsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name)
        (ws / "tasks").mkdir(); (ws / "results").mkdir()
        (ws / "state" / "task-event-handler-claims").mkdir(parents=True)
        (ws / "state" / "task-event-handler-fallbacks").mkdir(parents=True)
        (ws / "deliveries" / "worker-1").mkdir(parents=True)
        (ws / "tasks" / "task-held.txt").write_text("task: held\n")
        (ws / "tasks" / "task-free.txt").write_text("task: free\n")
        (ws / "deliveries" / "worker-1" / "task-held.claimed").write_text("")
        self.ws = ws

    def _pick(self, script_text: str) -> str:
        fn = _function_text("next_pending_task", script_text)
        harness = (
            "probe_optional_task_handler() { return 1; }\n" + fn + "\n"
            "next_pending_task\n"
        )
        env = {**os.environ,
               "TASKS_DIR": str(self.ws / "tasks"), "RESULTS_DIR": str(self.ws / "results"),
               "TASK_HANDLER_CLAIMS_DIR": str(self.ws / "state" / "task-event-handler-claims"),
               "TASK_HANDLER_FALLBACKS_DIR": str(self.ws / "state" / "task-event-handler-fallbacks"),
               "DELIVERIES_DIR": str(self.ws / "deliveries"),
               "NOTIFIER_PY": sys.executable,
               "DISPATCH_PY": str(REPO / "src" / "delivery" / "task_dispatch.py")}
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=30)
        return r.stdout.strip()

    def test_shipped_pick_skips_the_worker_held_task(self):
        self.assertEqual(self._pick(SCRIPT.read_text()), "task-free.txt")

    def test_pick_passes_the_deliveries_dir(self):
        text = SCRIPT.read_text()
        self.assertIn('--deliveries-dir "$DELIVERIES_DIR"', _function_text("next_pending_task", text))

    def test_without_the_argument_the_held_task_is_picked(self):
        # Oracle: the pre-fix function selects the held task (older by mtime, same
        # priority), so the shipped-pick assertion above discriminates.
        held = self.ws / "tasks" / "task-held.txt"
        os.utime(held, (1, 1))
        text = SCRIPT.read_text().replace(' --deliveries-dir "$DELIVERIES_DIR"', "")
        self.assertEqual(self._pick(text), "task-held.txt")
        self.assertEqual(self._pick(SCRIPT.read_text()), "task-free.txt")

if __name__ == "__main__":
    unittest.main()
