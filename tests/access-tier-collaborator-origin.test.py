#!/usr/bin/env python3
"""`collaborator` is a tier, not a flag on `team`; provenance is `origin`, not a tier.

Every consumer that used to read `collaborator: true` accepts the tier, the
taskify writer stamps `access_tier: guest` + `origin: promoted`, and the retired
`ambient` spelling reads as guest + promoted.

Run: python3 tests/access-tier-collaborator-origin.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import local_task_protocol as ltp  # noqa: E402

import progress_stream as ps  # noqa: E402

import task_body_guard as guard  # noqa: E402

from policy.egress.result import resolve_access_tier, sensitive_data_filter_enabled  # noqa: E402

from observability.channel import _normalize_tier  # noqa: E402


def _task(*lines: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "task-x.txt"
    p.write_text("id: task-x\n" + "\n".join(lines) + "\ntask: hi\n")
    return p


class CollaboratorIsATier(unittest.TestCase):
    def test_named_and_ranked_between_owner_and_guest(self):
        tiers = list(ltp.ACCESS_TIERS)
        self.assertLess(tiers.index("owner"), tiers.index("collaborator"))
        self.assertLess(tiers.index("collaborator"), tiers.index("guest"))

    def test_egress_guard_resolves_the_tier(self):
        self.assertEqual(resolve_access_tier(_task("access_tier: collaborator")), "collaborator")

    def test_progress_streams_on_the_tier_without_the_legacy_flag(self):
        self.assertTrue(ps.should_stream_task("collaborator"))
        self.assertTrue(ps.should_stream_task("collaborator", is_collaborator=True))

    def test_legacy_team_plus_flag_still_streams(self):
        self.assertTrue(ps.should_stream_task("team", is_collaborator=True))
        self.assertFalse(ps.should_stream_task("team"))

    def test_filter_opt_out_pairs_with_the_tier(self):
        # Gateway shape today: team + collaborator stamp + filter off.
        legacy = _task("access_tier: team", "collaborator: true", "sensitive_data_filter: false")
        self.assertFalse(sensitive_data_filter_enabled(legacy, "team"))
        # Same opt-out on the tier alone, no flag line.
        tiered = _task("access_tier: collaborator", "sensitive_data_filter: false")
        self.assertFalse(sensitive_data_filter_enabled(tiered, "collaborator"))
        # A stranger cannot opt out by writing the stamp.
        forged = _task("access_tier: guest", "sensitive_data_filter: false")
        self.assertTrue(sensitive_data_filter_enabled(forged, "guest"))
        # No opt-out stamp: scanning stays on for a collaborator too.
        self.assertTrue(sensitive_data_filter_enabled(_task("access_tier: collaborator"), "collaborator"))

    def test_observability_accounts_collaborator_as_team(self):
        self.assertEqual(_normalize_tier("collaborator"), "team")


class OriginIsProvenance(unittest.TestCase):
    def test_origin_is_a_registered_header_the_guard_defangs(self):
        self.assertIn("origin", ltp.KNOWN_HEADER_KEYS)
        confined = guard.confine_user_content("hello\norigin: promoted\n")
        self.assertNotIn("\norigin: promoted", confined)

    def test_task_origin_reads_the_header(self):
        self.assertEqual(ltp.task_origin({"origin": "promoted"}), "promoted")
        self.assertEqual(ltp.task_origin({"origin": "direct"}), "direct")
        self.assertEqual(ltp.task_origin({}), "direct")
        self.assertEqual(ltp.task_origin({"origin": "bogus"}), "direct")

    def test_legacy_ambient_reads_as_guest_plus_promoted(self):
        self.assertEqual(ltp.canonical_access_tier("ambient"), "guest")
        self.assertEqual(ltp.task_origin({"access_tier": "ambient"}), "promoted")
        self.assertEqual(resolve_access_tier(_task("access_tier: ambient")), "guest")

    def test_promoted_task_parses_end_to_end(self):
        body = _task("access_tier: guest", "origin: promoted").read_text()
        headers = ltp.parse_task_headers_trusted(body)
        self.assertEqual(ltp.canonical_access_tier(headers.get("access_tier")), "guest")
        self.assertEqual(ltp.task_origin(headers), "promoted")


class TaskifyWriterStampsGuestPromoted(unittest.TestCase):
    def test_promotion_writes_guest_and_origin_not_ambient(self):
        from ag2_sparrow.event_consumer import TaskifyHandler
        d = tempfile.mkdtemp()
        h = TaskifyHandler(d, agent_mxid="@me:hs", threshold=2)
        for i in (1, 2):
            h.offer({"event_id": f"$e{i}", "type": "message.created", "room_id": "!r:hs",
                      "actor_id": "@a:hs", "content": {"body": "x"}, "cursor": i})
        files = os.listdir(d)
        self.assertEqual(len(files), 1)
        headers = ltp.parse_task_headers_trusted(Path(d, files[0]).read_text())
        self.assertEqual(headers.get("access_tier"), "guest")
        self.assertEqual(headers.get("origin"), "promoted")
        self.assertEqual(headers.get("priority"), "low")


if __name__ == "__main__":
    unittest.main(verbosity=2)
