#!/usr/bin/env python3
"""Channel-aware dedup: result_markers.dedup_cross_channel_target.

A `[deduped: task-X]` result silently archives the deduped task and lets the
holder task-X carry the reply. But the holder's reply goes to ITS channel — so
a cross-channel dedup leaves the asking channel silent. dedup_cross_channel_target
returns the holder's channel when it differs (→ bridge posts a pointer), else
None (→ keep silent archive). All ids are FICTITIOUS. Run:
  python3 tests/dedup-cross-channel.test.py
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from result_markers import (  # noqa: E402
    dedup_cross_channel_target,
    dedup_requeue_count,
    build_requeued_task,
    parse_markers,
)

ASK_CH = "900000000000000001"      # channel where the deduped question arrived
HOLDER_CH = "900000000000000002"   # channel the holder task came from


def _holder(ch):
    return (
        "id: task-holder\n"
        "timestamp: 2026-06-22T00:00:00Z\n"
        "task: [Discord @owner] some question\n"
        f"channel_id: {ch}\n"
        "access_tier: owner\n"
    )


class DedupCrossChannel(unittest.TestCase):
    def test_cross_channel_returns_holder(self):
        # Holder came from a different channel → return it so a pointer is posted.
        self.assertEqual(
            dedup_cross_channel_target(ASK_CH, _holder(HOLDER_CH)), HOLDER_CH
        )

    def test_same_channel_returns_none(self):
        # Intra-channel consolidation (the common case) → keep silent archive.
        self.assertIsNone(dedup_cross_channel_target(ASK_CH, _holder(ASK_CH)))

    def test_int_vs_str_channel_is_same(self):
        # Bridge passes channel.id as int; holder file is str. Must compare equal.
        self.assertIsNone(dedup_cross_channel_target(int(ASK_CH), _holder(ASK_CH)))

    def test_int_vs_str_cross_channel(self):
        self.assertEqual(
            dedup_cross_channel_target(int(ASK_CH), _holder(HOLDER_CH)), HOLDER_CH
        )

    def test_holder_without_channel_id_returns_none(self):
        self.assertIsNone(
            dedup_cross_channel_target(ASK_CH, "id: task-holder\ntask: x\n")
        )

    def test_empty_or_missing_holder_returns_none(self):
        self.assertIsNone(dedup_cross_channel_target(ASK_CH, None))
        self.assertIsNone(dedup_cross_channel_target(ASK_CH, ""))

    def test_channel_id_not_matched_in_prose(self):
        # Only a real line-anchored `channel_id:` field counts, not prose.
        body = "task: mentions channel_id: 123 inline but no real field\n"
        self.assertIsNone(dedup_cross_channel_target(ASK_CH, body))

    def test_parse_markers_still_yields_deduped_extra(self):
        # Guard the contract this feature relies on: deduped → extra = holder id.
        r = parse_markers("[deduped: task-holder]")
        skip = [a for a in r.actions if a.kind == "skip"]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0].value, "deduped")
        self.assertEqual(skip[0].extra, "task-holder")


ORIG_TASK = (
    "id: task-orig\n"
    "timestamp: 2026-06-22T00:00:00Z\n"
    f"task: [Discord @owner] please do X\n"
    "source: discord\n"
    f"channel_id: {ASK_CH}\n"
    "access_tier: owner\n"
    "priority: normal\n"
)


class DedupRequeueGuard(unittest.TestCase):
    def test_requeue_count_absent_is_zero(self):
        self.assertEqual(dedup_requeue_count(ORIG_TASK), 0)
        self.assertEqual(dedup_requeue_count(None), 0)
        self.assertEqual(dedup_requeue_count(""), 0)

    def test_requeue_count_parsed(self):
        self.assertEqual(
            dedup_requeue_count(ORIG_TASK + "dedup_requeue_count: 1\n"), 1
        )
        self.assertEqual(
            dedup_requeue_count(ORIG_TASK + "dedup_requeue_count: 3\n"), 3
        )

    def test_build_requeued_sets_new_id(self):
        out = build_requeued_task(ORIG_TASK, "task-new", 1, ASK_CH, "task-holder")
        self.assertIn("id: task-new", out)
        self.assertNotIn("id: task-orig", out)

    def test_build_requeued_sets_count(self):
        out = build_requeued_task(ORIG_TASK, "task-new", 1, ASK_CH, "task-holder")
        self.assertEqual(dedup_requeue_count(out), 1)
        # round-trips: a re-queued task that comes back is detected as count>=1
        out2 = build_requeued_task(out, "task-new2", 2, ASK_CH, "task-holder")
        self.assertEqual(dedup_requeue_count(out2), 2)
        self.assertEqual(out2.count("dedup_requeue_count:"), 1)  # not duplicated

    def test_build_requeued_preserves_routing_fields(self):
        out = build_requeued_task(ORIG_TASK, "task-new", 1, ASK_CH, "task-holder")
        self.assertIn(f"channel_id: {ASK_CH}", out)
        self.assertIn("access_tier: owner", out)
        self.assertIn("please do X", out)

    def test_build_requeued_appends_trusted_instruction(self):
        out = build_requeued_task(ORIG_TASK, "task-new", 1, ASK_CH, "task-holder")
        self.assertIn("===SUTANDO SYSTEM INSTRUCTIONS", out)
        self.assertIn("per-channel only", out)
        self.assertIn(f"<#{ASK_CH}>", out)        # tells core to answer in-channel
        self.assertIn("task-holder", out)         # names the bad holder

    def test_build_requeued_is_still_parseable_task(self):
        # The re-queued content still starts with the id/field block.
        out = build_requeued_task(ORIG_TASK, "task-new", 1, ASK_CH, "task-holder")
        self.assertTrue(out.startswith("id: task-new\n"))


if __name__ == "__main__":
    unittest.main()
