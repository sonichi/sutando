#!/usr/bin/env python3
"""`source` names the instance, `channel_kind` names the shape.

The split's whole point is that a consumer asking "what shape is this" keeps
getting the right answer for an envelope written before the split existed.

Run: python3 tests/channel-kind.test.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from channel_kind import is_kind, kind_of  # noqa: E402
from local_task_protocol import KNOWN_HEADER_KEYS, parse_task_headers  # noqa: E402

SHAPES = frozenset({"telegram", "slack", "ag2space", "phone", "voice"})


class TestKind(unittest.TestCase):
    def test_a_pre_split_envelope_still_answers(self):
        """The migration's load-bearing case: no channel_kind, kind in source."""
        self.assertEqual(kind_of({"source": "ag2space"}), "ag2space")
        self.assertTrue(is_kind({"source": "ag2space"}, SHAPES))

    def test_a_post_split_envelope_reads_the_new_field(self):
        t = {"source": "ag2space.local", "channel_kind": "ag2space"}
        self.assertEqual(kind_of(t), "ag2space")
        self.assertTrue(is_kind(t, SHAPES))

    def test_two_homeservers_are_now_distinguishable(self):
        a = {"source": "ag2space.local", "channel_kind": "ag2space"}
        b = {"source": "ag2.space", "channel_kind": "ag2space"}
        self.assertNotEqual(a["source"], b["source"])
        self.assertEqual(kind_of(a), kind_of(b), "same shape, different instance")

    def test_a_distinct_source_alone_would_have_broken_the_membership_test(self):
        """Why the second field exists rather than just renaming source."""
        self.assertFalse(is_kind({"source": "ag2space.local"}, SHAPES))
        self.assertTrue(is_kind({"source": "ag2space.local",
                                 "channel_kind": "ag2space"}, SHAPES))

    def test_absent_and_blank_are_not_a_kind(self):
        for t in ({}, {"source": "   "}, {"source": None}, None, "nope"):
            self.assertEqual(kind_of(t), "")
            self.assertFalse(is_kind(t, SHAPES))

    def test_comparison_is_case_insensitive(self):
        self.assertTrue(is_kind({"channel_kind": "AG2Space"}, SHAPES))

    def test_it_reads_the_type_the_production_caller_passes(self):
        """parse_task_headers returns TaskHeaders, not a dict; an isinstance
        check answers "" for every real task while unit dicts keep passing."""
        h = parse_task_headers("id: t1\nsource: ag2space\ntask: body\n")
        self.assertNotIsInstance(h, dict)
        self.assertEqual(kind_of(h), "ag2space")
        self.assertTrue(is_kind(h, SHAPES))

    def test_the_header_survives_the_allowlist(self):
        """Dropped here, the field would vanish silently between bridges."""
        self.assertIn("channel_kind", KNOWN_HEADER_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=0)
