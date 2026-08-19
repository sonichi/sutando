#!/usr/bin/env python3
"""Health visibility for Discord terminal receipts held fail-closed."""
from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "health_check_terminal_receipts", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hc)


class TerminalReceiptHealthTest(unittest.TestCase):
    def _check(self, workspace: Path) -> dict:
        with mock.patch.object(hc, "WORKSPACE_DIR", workspace):
            return hc.check_terminal_receipts()

    @staticmethod
    def _root(workspace: Path) -> Path:
        return workspace / "results" / ".outbox-discord-task-results"

    def test_absent_store_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._check(Path(tmp))
        self.assertEqual(check["status"], "ok", check)
        self.assertIn("0 terminal receipt(s)", check["detail"])

    def test_valid_receipt_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            hc.outbox.record_terminal_receipt(
                self._root(workspace), "task-valid",
                hc.outbox.TerminalDisposition.DELIVERED)
            check = self._check(workspace)
        self.assertEqual(check["status"], "ok", check)
        self.assertIn("1 terminal receipt(s)", check["detail"])

    def test_corrupt_receipt_warns_and_survives_the_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = self._root(workspace)
            hc.outbox.record_terminal_receipt(
                root, "task-corrupt", hc.outbox.TerminalDisposition.DELIVERED)
            path = hc.outbox._terminal_receipt_path(root, "task-corrupt", 0)
            path.write_bytes(b"corrupt")

            check = self._check(workspace)

            self.assertEqual(check["status"], "warn", check)
            self.assertIn("1 unreadable or corrupt receipt entry", check["detail"])
            self.assertIn("held fail-closed", check["detail"])
            self.assertIn("confirm the delivery outcome", check["detail"])
            self.assertTrue(path.exists())
            self.assertEqual(
                hc.outbox.read_terminal_receipt(root, "task-corrupt").state,
                hc.outbox.TerminalReceiptState.UNKNOWN)

    def test_incomplete_or_failed_inspection_warns(self):
        report = hc.outbox.TerminalReceiptCleanup(incomplete=True)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hc.outbox, "cleanup_terminal_receipts",
                                  return_value=report):
            check = self._check(Path(tmp))
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("inspection incomplete", check["detail"])

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hc.outbox, "cleanup_terminal_receipts",
                                  side_effect=PermissionError("denied")):
            check = self._check(Path(tmp))
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("could not inspect", check["detail"])

    def test_probe_is_wired_into_run_all_checks(self):
        tree = ast.parse((REPO / "src" / "health-check.py").read_text())
        run_all = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef)
                       and node.name == "run_all_checks")
        calls = [node.func.id for node in ast.walk(run_all)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)]
        self.assertEqual(calls.count("check_terminal_receipts"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
