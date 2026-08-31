#!/usr/bin/env python3
"""claim_task edge branches: idle-threshold env parsing, handler IO
failures, malformed handler records, the unexpected-errno report, and the
CLI dispatch.

Run: python3 tests/claim-task-branches.test.py   (stdlib only)
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import claim_task  # noqa: E402


def _ws(td: str) -> Path:
    ws = Path(td)
    (ws / "tasks").mkdir()
    (ws / "state" / "cores").mkdir(parents=True)
    return ws


class IdleThresholdEnvTests(unittest.TestCase):
    def _with(self, value):
        old = os.environ.pop("SUTANDO_CORE_IDLE_THRESHOLD_SEC", None)
        try:
            if value is not None:
                os.environ["SUTANDO_CORE_IDLE_THRESHOLD_SEC"] = value
            return claim_task._idle_threshold_sec()
        finally:
            os.environ.pop("SUTANDO_CORE_IDLE_THRESHOLD_SEC", None)
            if old is not None:
                os.environ["SUTANDO_CORE_IDLE_THRESHOLD_SEC"] = old

    def test_unset_uses_default(self):
        self.assertEqual(self._with(None),
                         claim_task.DEFAULT_IDLE_THRESHOLD_SEC)

    def test_non_numeric_uses_default(self):
        self.assertEqual(self._with("abc"),
                         claim_task.DEFAULT_IDLE_THRESHOLD_SEC)

    def test_negative_uses_default(self):
        self.assertEqual(self._with("-5"),
                         claim_task.DEFAULT_IDLE_THRESHOLD_SEC)

    def test_positive_wins(self):
        self.assertEqual(self._with("77"), 77)


class HandlerIOTests(unittest.TestCase):
    def test_write_handler_failure_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            blocked = Path(td) / "not-a-dir"
            blocked.write_text("file blocks the parent dir")
            claim_task._write_handler(blocked / "h.json", "1", 1.0)

    def test_is_alive_missing_heartbeat_is_dead(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            self.assertFalse(claim_task._is_alive(ws, "9", time.time()))

    def test_malformed_fresh_handler_falls_back_to_race_claim(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            (ws / "tasks" / "task-m1.txt").write_text("x")
            hp = claim_task._handler_path(ws, "chan")
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(json.dumps({"last_handled_at": time.time()}))
            got = claim_task.claim_with_affinity("m1", "2", "chan",
                                                 workspace=ws)
            self.assertIsNotNone(got)
            self.assertEqual(json.loads(hp.read_text())["core_id"], "2")

    def test_fresh_alive_handler_claims_and_refreshes(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            (ws / "tasks" / "task-m2.txt").write_text("x")
            (ws / "state" / "cores" / "core-1.alive").write_text("beat")
            hp = claim_task._handler_path(ws, "chan")
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(json.dumps(
                {"core_id": "1", "last_handled_at": time.time()}))
            self.assertIsNone(claim_task.claim_with_affinity(
                "m2", "2", "chan", workspace=ws))
            got = claim_task.claim_with_affinity("m2", "1", "chan",
                                                 workspace=ws)
            self.assertIsNotNone(got)
            self.assertTrue(got.name.endswith(".claimed-core-1.txt"))


class UnexpectedErrnoTests(unittest.TestCase):
    def test_non_enoent_rename_failure_reports_errno(self):
        if os.geteuid() == 0:
            self.skipTest("EACCES not enforceable as root")
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            (ws / "tasks" / "task-p1.txt").write_text("x")
            os.chmod(ws / "tasks", stat.S_IRUSR | stat.S_IXUSR)
            try:
                err = io.StringIO()
                with redirect_stderr(err):
                    got = claim_task.claim_plain("p1", "1", workspace=ws)
                self.assertIsNone(got)
                self.assertIn("unexpected errno", err.getvalue())
            finally:
                os.chmod(ws / "tasks", stat.S_IRWXU)


class CliTests(unittest.TestCase):
    def _run(self, argv, ws):
        out, err = io.StringIO(), io.StringIO()
        old = claim_task.resolve_workspace
        claim_task.resolve_workspace = lambda: ws
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = claim_task._main(["claim_task.py", *argv])
        finally:
            claim_task.resolve_workspace = old
        return rc, out.getvalue(), err.getvalue()

    def test_usage_error(self):
        rc, _, err = self._run([], None)
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_plain_claim_wins_prints_path(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            (ws / "tasks" / "task-c1.txt").write_text("x")
            rc, out, _ = self._run(["c1", "1"], ws)
            self.assertEqual(rc, 0)
            self.assertIn("task-c1.claimed-core-1.txt", out)

    def test_missing_task_is_lost_race(self):
        with tempfile.TemporaryDirectory() as td:
            rc, _, _ = self._run(["nope", "1"], _ws(td))
            self.assertEqual(rc, 1)

    def test_invalid_id_is_validation_error(self):
        with tempfile.TemporaryDirectory() as td:
            rc, _, err = self._run(["../evil", "1"], _ws(td))
            self.assertEqual(rc, 2)
            self.assertIn("claim_task:", err)

    def test_affinity_dispatch_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _ws(td)
            (ws / "tasks" / "task-c2.txt").write_text("x")
            rc, out, _ = self._run(["c2", "1", "chan"], ws)
            self.assertEqual(rc, 0)
            self.assertIn(".claimed-core-1.txt", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
