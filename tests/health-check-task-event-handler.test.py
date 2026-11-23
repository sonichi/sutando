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
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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

    def test_executable_directory_is_down(self):
        d = self.ws / "router-dir"
        d.mkdir()
        d.chmod(0o755)
        self.declare(str(d))
        self.assertEqual(hc.check_task_event_handler(self.ws)["status"], "down")

    def test_run_all_checks_carries_the_row(self):
        row = {"name": "task-event-handler", "status": "down", "detail": "sentinel"}
        with mock.patch.object(hc, "check_task_event_handler", return_value=row) as probe:
            checks = hc.run_all_checks()
        probe.assert_called_once_with()
        self.assertIn(row, checks)

    def test_watcher_lookup_refuses_an_executable_directory(self):
        d = self.ws / "router-dir"
        d.mkdir()
        d.chmod(0o755)
        self.declare(str(d))
        lookup = REPO / "src" / "agent" / "task-event-handler-lookup.sh"
        run = subprocess.run(
            ["bash", "-c", 'source "$1"; read_task_event_handler_config "$2"', "_", str(lookup), str(self.cfg)],
            capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "")

    def test_watcher_lookup_refuses_a_pinned_directory(self):
        d = self.ws / "pinned-dir"
        d.mkdir()
        d.chmod(0o755)
        lookup = REPO / "src" / "agent" / "task-event-handler-lookup.sh"
        run = subprocess.run(
            ["bash", "-c", 'source "$1"; task_event_handler "$2"', "_", str(lookup), str(self.cfg)],
            capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SUTANDO_TASK_EVENT_HANDLER": str(d)})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "")

    def test_a_working_pin_overrides_a_stale_declaration(self):
        pin = self.ws / "pinned-router"
        pin.write_text("#!/bin/sh\n")
        pin.chmod(0o755)
        self.declare(str(self.ws / "gone" / "router"))
        with mock.patch.dict(os.environ, {"SUTANDO_TASK_EVENT_HANDLER": str(pin)}):
            self.assertIsNone(hc.check_task_event_handler(self.ws))
        with mock.patch.dict(os.environ, {"SUTANDO_TASK_EVENT_HANDLER": str(self.ws / "gone-pin")}):
            self.assertEqual(hc.check_task_event_handler(self.ws)["status"], "down")

    def test_unparseable_config_fails(self):
        self.cfg.write_text("{not json")
        self.assertEqual(hc.check_task_event_handler(self.ws)["status"], "down")


if __name__ == "__main__":
    unittest.main()
