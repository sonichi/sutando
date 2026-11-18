#!/usr/bin/env python3
"""worker-inbox-priority.test.py under a runtime harness: the harness file's paths reach the children
and the diagnostics are copied beside it, while anything else in that file (a token) reaches nothing.

Run: python3 tests/worker-inbox-priority-harness.test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "tests" / "_helpers" / "sentinel_audit.py"
TARGET = REPO / "tests" / "worker-inbox-priority.test.py"
CASE = "WorkerStartupTest.test_ordinary_core_inbox_keeps_priority_fifo_and_body_boundary"
TOKEN = "harness-sentinel-token"


class RuntimeHarness(unittest.TestCase):
    @unittest.skipUnless(shutil.which("fswatch"), "the target needs fswatch")
    def test_harness_paths_reach_children_and_diagnostics_but_its_token_does_not(self):
        harness = Path(tempfile.mkdtemp(prefix="h-")).resolve()
        marker = harness / "harness-bin"
        (harness / "environment.json").write_text(json.dumps(
            {"PATH": f"{marker}{os.pathsep}{os.environ['PATH']}", "REMOTE_TASK_TOKEN": TOKEN}))
        out = harness / "audit.json"
        got = subprocess.run([sys.executable, str(AUDIT), str(out), str(TARGET), CASE], cwd=REPO,
                             env=hermetic_env(SUTANDO_RUNTIME_SOCKET=str(harness / "api.sock")),
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(got.returncode, 0, got.stdout[-2000:] + got.stderr[-2000:])
        record = json.loads(out.read_text())
        self.assertTrue(record["child_envs"], "the case started no child")
        self.assertTrue(all(str(marker) in e.get("PATH", "").split(os.pathsep) for e in record["child_envs"]),
                        "a child did not get the harness PATH")
        self.assertEqual([e for e in record["child_envs"] + [record["final_env"]] if TOKEN in json.dumps(e)], [])
        self.assertTrue(list((harness / "diagnostics").glob("worker-priority-*.json")), "no diagnostics copied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
