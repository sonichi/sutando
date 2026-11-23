#!/usr/bin/env python3
"""health-check fails when the declared task-event handler is gone.

The core watcher holds every new task while the handler config names a file that
is missing or not executable, and nothing else surfaces it.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc_teh", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc_teh"] = hc
_spec.loader.exec_module(hc)


class TaskEventHandlerCheck(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="hc-teh-"))
        (self.ws / "state").mkdir()
        self.cfg = self.ws / "state" / "task-event-handler.json"

    def declare(self, handler):
        self.cfg.write_text(json.dumps({"handler": handler}))

    def test_no_config_is_silent(self):
        self.assertIsNone(hc.check_task_event_handler(self.ws))

    def test_live_handler_is_silent(self):
        h = self.ws / "router"
        h.write_text("#!/bin/sh\n")
        h.chmod(0o755)
        self.declare(str(h))
        self.assertIsNone(hc.check_task_event_handler(self.ws))

    def test_missing_handler_fails(self):
        self.declare(str(self.ws / "gone" / "router"))
        row = hc.check_task_event_handler(self.ws)
        self.assertEqual(row["status"], "down")
        self.assertEqual(row["name"], "task-event-handler")
        self.assertIn("holds every new task", row["detail"])

    def test_unparseable_config_fails(self):
        self.cfg.write_text("{not json")
        self.assertEqual(hc.check_task_event_handler(self.ws)["status"], "down")


if __name__ == "__main__":
    unittest.main()
