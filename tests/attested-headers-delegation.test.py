#!/usr/bin/env python3
"""Structural pins: every tier/routing consumer reads task headers through
task_envelope.attested_task_headers and owns no copy of the strict/trusted rule.
A behaviour test cannot see a duplicate that currently agrees; this can."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONSUMERS = (
    REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_ask.py",
    REPO / "skills" / "worker-pool" / "scripts" / "worker_picker_commands.py",
)
OWNER = REPO / "src" / "task_envelope.py"


class Delegation(unittest.TestCase):
    def test_each_consumer_calls_the_shared_helper(self):
        for p in CONSUMERS:
            with self.subTest(p.name):
                self.assertRegex(p.read_text(), r"\battested_task_headers\s*\(")

    def test_no_consumer_reimplements_the_shape_rule(self):
        banned = re.compile(r"parse_task_headers_(trusted|lenient)\s*\(|\bverify_text\s*\(")
        for p in CONSUMERS:
            with self.subTest(p.name):
                self.assertIsNone(banned.search(p.read_text()),
                                  f"{p.name} applies the strict/trusted rule itself")

    def test_the_owner_gates_the_trusted_scan_on_the_layout_marker(self):
        src = OWNER.read_text()
        body = src.split("def attested_task_headers", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"task_layout"', body)
        self.assertIn("parse_task_headers_trusted", body)
        self.assertIn("verify_text", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
