#!/usr/bin/env python3
"""Dashboard and active-task HTTP routes survive absent platform process tools."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_process_probe_test", REPO / "src" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get(module, path):
    handler = module.Handler.__new__(module.Handler)
    handler.path = path
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.do_GET()
    handler.send_response.assert_called_once_with(200)
    return handler, handler.wfile.getvalue().decode("utf-8")


class HttpProcessProbes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ws = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("workspace_default.resolve_workspace", return_value=self.ws))
        self.dashboard = load("dashboard")
        self.api = load("agent-api")
        self.stack.enter_context(patch.object(
            self.api, "personal_path", side_effect=lambda name, workspace: workspace / name))
        data = {
            "get_health": [], "get_activity": [], "get_outbox": [],
            "get_pending_count": {"open": 0, "done": 0},
            "get_score": "?", "get_use_case_matrix": "", "get_schedules": [],
            "get_system_stats": {
                "disk_free": "100GB", "battery": "--", "charging": False,
                "uptime": "00:00", "quota": {},
            },
        }
        for name, value in data.items():
            self.stack.enter_context(patch.object(self.dashboard, name, return_value=value))
        self.stack.enter_context(patch(
            "subprocess.run", side_effect=FileNotFoundError("process tools unavailable")))

    def test_dashboard_process_status_delegates_in_both_directions(self):
        for pids, expected in (([], "Sutando app not running"), (["42"], "Sutando app running")):
            with self.subTest(pids=pids), patch.object(
                self.dashboard, "find_pids", return_value=pids, create=True
            ) as probe:
                handler, body = get(self.dashboard, "/")
                probe.assert_called_once_with("(Sutando|MacOS)/Sutando")
                self.assertIn(expected, body)
                self.assertIn("<h2>Schedules</h2>", body)
                handler.send_header.assert_any_call("Content-Type", "text/html; charset=utf-8")

    def test_active_tasks_process_status_delegates_in_both_directions(self):
        for pids in ([], ["42"]):
            with self.subTest(pids=pids), patch.object(
                self.api, "find_pids", return_value=pids, create=True
            ) as probe:
                handler, body = get(self.api, "/tasks/active")
                probe.assert_called_once_with("watch-tasks")
                self.assertEqual(json.loads(body), {
                    "tasks": [], "watcher": bool(pids), "claude": False, "questions": [],
                })
                handler.send_header.assert_any_call("Content-Type", "application/json")
                handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")

    def test_missing_process_tools_do_not_abort_either_route(self):
        _, dashboard = get(self.dashboard, "/")
        self.assertIn("Sutando app not running", dashboard)
        _, active = get(self.api, "/tasks/active")
        self.assertFalse(json.loads(active)["watcher"])


if __name__ == "__main__":
    unittest.main()
