#!/usr/bin/env python3
"""Edge/resilience branches across the runtime-api views.

Each case pins a fail-safe branch that the flow suites do not reach:
unreadable files degrade to absent, empty identity/host config degrades to
empty answers, oversized or non-JSON capability payloads refuse loudly, and
a resolved human-action close is idempotent.

Run: python3 tests/runtime-api-edge-gaps.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
sys.path.insert(0, str(REPO / "src"))

# flake8: noqa: E402 — imports come after the sys.path bootstrap above
from tasks_view import TasksView
from agents_view import AgentsView
from schedules_view import SchedulesView
from runtime_view import RuntimeView
from ha_adapter import HumanActionAdapter
import capability_registry as capreg


class TasksViewEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = Path(self.tmp.name) / "tasks"
        self.results = Path(self.tmp.name) / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        self.v = TasksView(self.tasks, self.results, "@edge:test")

    def tearDown(self):
        for p in list(self.tasks.iterdir()) + list(self.results.iterdir()):
            p.chmod(0o644)
        self.tmp.cleanup()

    def test_status_done_when_result_exists(self):
        (self.results / "task-a1.txt").write_text("finished")
        self.assertEqual(self.v.status("task-a1")["state"], "done")

    def test_unreadable_task_file_degrades_to_absent_details(self):
        f = self.tasks / "task-b2.txt"
        f.write_text("id: task-b2\ntask: hi\n")
        f.chmod(0o000)
        # unreadable is indistinguishable from absent for a reader — never a crash
        try:
            self.v.details("task-b2")
        except OSError:
            self.fail("details must not raise on an unreadable task file")

    def test_unreadable_result_degrades_to_pending(self):
        f = self.results / "task-c3.txt"
        f.write_text("body")
        f.chmod(0o000)
        try:
            out = self.v.get_result("task-c3")
        except OSError:
            self.fail("get_result must not raise on an unreadable result")
        self.assertTrue(out is None or "body" not in json.dumps(out))


class AgentsViewEdges(unittest.TestCase):
    def test_empty_agent_id_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            v = AgentsView(td)
            self.assertIsNone(v.agent_status(""))


class SchedulesViewEdges(unittest.TestCase):
    def test_missing_crons_is_empty_never_error(self):
        with tempfile.TemporaryDirectory() as td:
            v = SchedulesView(Path(td) / "nope" / "crons.json")
            self.assertEqual(v.list_schedules()["schedules"], [])

    def test_configured_crons_list_with_computed_next_run(self):
        # the incomplete-extraction regression: schedule.list must actually
        # read a configured crons.json, not just no-op on a missing one
        with tempfile.TemporaryDirectory() as td:
            crons = Path(td) / "crons.json"
            crons.write_text(json.dumps([
                {"name": "beat", "cron": "*/5 * * * *",
                 "prompt_skill": "proactive-loop"},
                {"name": "loose", "loop": "dynamic", "prompt": "Run: sweep"},
            ]))
            rows = SchedulesView(crons).list_schedules()["schedules"]
            byname = {r["name"]: r for r in rows}
            self.assertEqual(byname["beat"]["kind"], "skill")
            self.assertIsNotNone(byname["beat"]["next_run_ts"])
            self.assertEqual(byname["loose"]["cron"], "")
            self.assertEqual(byname["loose"]["next_run"], "invalid")
            self.assertEqual(byname["loose"]["kind"], "prompt")
            self.assertIn("sweep", byname["loose"]["description"])

    def test_relative_formats_and_descriptions(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as td:
            crons = Path(td) / "crons.json"
            crons.write_text(json.dumps([
                {"name": "hourly", "cron": "30 */3 * * *",
                 "description": "own words"},
                {"name": "daily", "cron": "0 4 * * 1",
                 "prompt": "p" * 130},
                {"name": "sh", "cron": "0 4 * * *",
                 "shell_command": "echo hi"},
            ]))
            rows = SchedulesView(crons).list_schedules()["schedules"]
            byname = {r["name"]: r for r in rows}
            self.assertEqual(byname["hourly"]["description"], "own words")
            self.assertTrue(byname["daily"]["description"].endswith("…"))
            self.assertEqual(byname["sh"]["kind"], "shell")
            self.assertTrue(all(r["next_run_ts"] for r in rows))


class RuntimeViewEdges(unittest.TestCase):
    def test_no_host_label_gives_empty_own_beat(self):
        with tempfile.TemporaryDirectory() as td:
            v = RuntimeView(Path(td), host_label="")
            self.assertEqual(v._own_beat(), {})


class HaAdapterEdges(unittest.TestCase):
    def test_close_is_idempotent_on_missing_and_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            ha = HumanActionAdapter(td)
            ha.close("nope-action", "@o:x")  # missing: silently returns
            aid = ha.open_approval({"requestId": "req-1",
                                    "params": {"action": "x"}})
            ha.close(aid, "@o:x")
            ha.close(aid, "@o:x")  # second close: no longer pending, no raise


class CapabilityRegistryEdges(unittest.TestCase):
    def test_non_json_value_refuses_loudly(self):
        with self.assertRaises(ValueError):
            capreg._json_copy({"x": object()}, "input", 1024)

    def test_oversized_value_refuses_loudly(self):
        with self.assertRaises(ValueError):
            capreg._json_copy({"x": "y" * 4096}, "input", 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
