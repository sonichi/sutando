#!/usr/bin/env python3
"""doc_sync stamps a pool worker's edits with the worker's own id, in the same
order outbox_log uses. Run: python3 tests/agent-room-ops-doc-sync-writer-id.test.py"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_SKILL))
_spec = importlib.util.spec_from_file_location("doc_sync_writer", _SKILL / "doc_sync.py")
doc_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doc_sync)


class WriterId(unittest.TestCase):
    def test_a_worker_stamps_its_own_id_over_the_cores(self):
        env = {"SUTANDO_WORKER_ID": "d1fc9b10", "SUTANDO_CORE_ID": "core-air"}
        self.assertEqual(doc_sync.writer_id(None, env), "d1fc9b10")

    def test_a_worker_with_no_core_id_is_not_unknown(self):
        self.assertEqual(doc_sync.writer_id(None, {"SUTANDO_WORKER_ID": "d1fc9b10"}), "d1fc9b10")

    def test_the_core_keeps_its_core_id(self):
        self.assertEqual(doc_sync.writer_id(None, {"SUTANDO_CORE_ID": "core-air"}), "core-air")

    def test_an_explicit_writer_wins(self):
        env = {"SUTANDO_WORKER_ID": "d1fc9b10", "SUTANDO_CORE_ID": "core-air"}
        self.assertEqual(doc_sync.writer_id("me", env), "me")

    def test_blank_values_fall_through(self):
        env = {"SUTANDO_WORKER_ID": "  ", "SUTANDO_CORE_ID": "core-air"}
        self.assertEqual(doc_sync.writer_id(None, env), "core-air")
        self.assertEqual(doc_sync.writer_id(None, {}), "unknown")


class WriterHelp(unittest.TestCase):
    def test_the_writer_help_names_the_stamp_order(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            doc_sync.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        help_text = " ".join(out.getvalue().split())
        self.assertIn("SUTANDO_WORKER_ID, else SUTANDO_CORE_ID, else 'unknown'", help_text)


if __name__ == "__main__":
    unittest.main()
