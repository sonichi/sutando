#!/usr/bin/env python3
"""health-check's cli-wedge probe: absent socket / unreadable pane are readings
of nothing (ok), a static pane with work outstanding warns, a retry loop warns,
and idle never does. The tmux and heartbeat seams are replaced with fakes."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

IDLE = "❯ \n⏵⏵ bypass permissions on · 12:01:05 PM · 1 monitor\n"


class CliWedgeProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        (ws / "state").mkdir()
        (ws / "tasks").mkdir()
        self.ws = ws
        self._saved = (hc.WORKSPACE_DIR, hc._local_core_socket, hc._run_tmux)
        hc.WORKSPACE_DIR = ws
        hc._local_core_socket = lambda *a, **k: "/tmp/fake.sock"
        self.frames = [IDLE]
        hc._run_tmux = lambda sock, *args: SimpleNamespace(returncode=0, stdout=self.frames[min(self.calls(), len(self.frames) - 1)])
        self._n = 0

    def calls(self):
        n = self._n
        self._n += 1
        return n

    def tearDown(self):
        hc.WORKSPACE_DIR, hc._local_core_socket, hc._run_tmux = self._saved
        self.tmp.cleanup()

    def test_missing_detector_module_is_a_detail_not_a_failure(self):
        saved = sys.modules.get("cli_wedge")
        sys.modules["cli_wedge"] = None  # makes `import cli_wedge` raise
        try:
            c = hc.check_cli_wedge()
        finally:
            if saved is None:
                sys.modules.pop("cli_wedge", None)
            else:
                sys.modules["cli_wedge"] = saved
        self.assertEqual(c["status"], "ok")
        self.assertIn("detector unavailable", c["detail"])

    def test_no_local_core_is_ok_and_says_so(self):
        hc._local_core_socket = lambda *a, **k: None
        c = hc.check_cli_wedge()
        self.assertEqual((c["name"], c["status"]), ("cli-wedge", "ok"))
        self.assertIn("no local core pane", c["detail"])

    def test_unreadable_pane_is_ok_not_a_verdict(self):
        hc._run_tmux = lambda *a: None
        c = hc.check_cli_wedge()
        self.assertEqual(c["status"], "ok")
        self.assertIn("not a verdict", c["detail"])

    def test_idle_pane_never_warns(self):
        for _ in range(3):
            c = hc.check_cli_wedge()
        self.assertEqual(c["status"], "ok")
        self.assertIn("idle", c["detail"])
        self.assertEqual(c["evidence"]["sample_count"], 3)

    def test_static_pane_with_work_outstanding_warns(self):
        (self.ws / "state" / "core-status.json").write_text(json.dumps({"status": "running"}))
        for _ in range(3):
            c = hc.check_cli_wedge()
        self.assertEqual(c["status"], "warn")
        self.assertIn("static-with-work", c["detail"])
        self.assertTrue(c["evidence"]["work_outstanding"])
        self.assertIn("reads the pane, not the process", c["detail"])

    def test_retry_loop_warns_even_when_the_pane_moves(self):
        self.frames = [f"Connection error. Retrying in {3 * (i % 3)}s (attempt {i}/10) 04:2{i % 10}:11\n" for i in range(12)]
        (self.ws / "tasks" / "task-1.txt").write_text("task: x\n")
        for _ in range(12):
            c = hc.check_cli_wedge()
        self.assertEqual(c["status"], "warn")
        self.assertIn("retry-loop", c["detail"])
        self.assertIn("retrying", c["evidence"]["matched_patterns"])

    def test_working_pane_is_ok(self):
        self.frames = [f"● wrote {chr(97 + i)}.ts\n" for i in range(12)]
        for _ in range(12):
            c = hc.check_cli_wedge()
        self.assertEqual(c["status"], "ok")
        self.assertIn("working", c["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
