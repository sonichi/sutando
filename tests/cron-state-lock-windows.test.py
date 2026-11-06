#!/usr/bin/env python3
"""Production cron-state lock contention across supported platforms."""

import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cron_runner_lock_test", REPO / "src" / "cron-runner.py"
)
cron_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron_runner)


class CronStateLock(unittest.TestCase):
    def test_contender_waits_for_the_production_lock(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "cron-runner-state.json"
            acquired = threading.Event()

            def contend():
                with cron_runner._state_lock(state):
                    acquired.set()

            with cron_runner._state_lock(state):
                worker = threading.Thread(target=contend)
                worker.start()
                self.assertFalse(acquired.wait(0.2))

            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(acquired.is_set())


if __name__ == "__main__":
    unittest.main()
