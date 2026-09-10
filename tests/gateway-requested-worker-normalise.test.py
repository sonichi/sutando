#!/usr/bin/env python3
"""The broker says `target_worker`; this envelope says `requested_worker`.

Two names for one fact, and an allowlist that drops what it does not know, so
the field was produced and then silently discarded in transit. The boundary is
where the names are reconciled, and the envelope keeps exactly one of them.

Run: python3 tests/gateway-requested-worker-normalise.test.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import KNOWN_HEADER_KEYS, parse_task_headers  # noqa: E402

WRITER = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
          / "remote_gateway_bridge.py").read_text(encoding="utf-8")
W = "a" * 32


def envelope(**fields) -> str:
    """The writer's own normalisation rule, applied to a task dict."""
    rw = fields.get("requested_worker") or fields.get("target_worker")
    lines = [f"id: {fields.get('id', 't1')}"]
    if rw:
        lines.append(f"requested_worker: {rw}")
    lines.append("task: body")
    return "\n".join(lines) + "\n"


class TestNormalisation(unittest.TestCase):
    def test_the_writer_reads_both_names(self):
        """Pinned against the source: a rename there must fail here."""
        self.assertRegex(
            WRITER,
            re.compile(r'task\.get\("requested_worker"\)\s*or\s*task\.get\("target_worker"\)'))

    def test_the_brokers_name_reaches_the_core(self):
        h = parse_task_headers(envelope(target_worker=W))
        self.assertEqual(h.get("requested_worker"), W)

    def test_our_own_name_still_wins_when_both_are_present(self):
        h = parse_task_headers(envelope(requested_worker=W, target_worker="b" * 32))
        self.assertEqual(h.get("requested_worker"), W)

    def test_neither_present_emits_no_header(self):
        self.assertIsNone(parse_task_headers(envelope()).get("requested_worker"))

    def test_only_one_name_survives_the_allowlist(self):
        """Why normalising is required rather than optional: the other name is
        dropped, and a dropped header looks exactly like an unaddressed task."""
        self.assertIn("requested_worker", KNOWN_HEADER_KEYS)
        self.assertNotIn("target_worker", KNOWN_HEADER_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=0)
