#!/usr/bin/env python3
"""A dedup that folds one sender's task into another's must not be honoured.

`dedup_cross_channel_target` asks whether the reply left the ROOM. In a shared
multi-member room every sender carries the same `channel_id`, so it returns
None and the dedup is honoured — the holder is answered, the asker hears
nothing, and every existence check passes. Measured: 74 such silences in one
20-member room across 6 senders.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dedup_recovery import plan_dedup_recovery  # noqa: E402

ROOM = "!bKQkxfOrHZwejIyDLI:ag2.space"


def _task(tid, user, channel=ROOM):
    # Header shape the bridges actually write: `task:` stays last.
    return (f"id: {tid}\nsource: ag2space\nchannel_id: {channel}\n"
            f"user_id: {user}\naccess_tier: team\ntask: original ask\n")


class CrossSenderPredicate(unittest.TestCase):
    # imported here, not at module scope, so the behavioural class below
    # still runs (and fails on its VERDICT) against a tree without them.
    def test_flags_a_different_sender(self):
        from result_markers import dedup_cross_sender_target
        self.assertEqual(
            dedup_cross_sender_target("@alice:ag2.space", _task("task-h", "@bob:ag2.space")),
            "@bob:ag2.space",
        )

    def test_quiet_for_the_same_sender(self):
        from result_markers import dedup_cross_sender_target
        self.assertIsNone(
            dedup_cross_sender_target("@alice:ag2.space", _task("task-h", "@alice:ag2.space")))

    def test_quiet_when_the_holder_has_no_user_id(self):
        from result_markers import dedup_cross_sender_target
        self.assertIsNone(dedup_cross_sender_target("@alice:ag2.space", "id: task-h\ntask: x\n"))

    def test_task_user_id_reads_the_header(self):
        from result_markers import task_user_id
        self.assertEqual(task_user_id(_task("task-a", "@alice:ag2.space")), "@alice:ag2.space")
        self.assertIsNone(task_user_id(None))


class PlanRefusesCrossSenderDedup(unittest.TestCase):
    """The regression: same room, different senders, holder DID deliver."""

    def _plan(self, holder_user, holder_result="a real, delivered answer\n", count=0):
        # a fresh workspace per call: several plans run inside one test
        d = Path(self.tmp) / f"ws{self._n}"
        self._n += 1
        tasks, results = d / "tasks", d / "results"
        tasks.mkdir(parents=True), results.mkdir(parents=True)
        victim = _task("task-victim", "@alice:ag2.space")
        if count:
            victim = victim.replace("access_tier: team\n", f"access_tier: team\ndedup_requeue_count: {count}\n")
        (tasks / "task-victim.txt").write_text(victim)
        (tasks / "task-holder.txt").write_text(_task("task-holder", holder_user))
        (results / "task-holder.txt").write_text(holder_result)
        return plan_dedup_recovery(
            results, tasks, "task-victim", "task-holder", ROOM, "task-new",
        )

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self._n = 0

    def tearDown(self):
        self._td.cleanup()

    def test_same_room_different_sender_is_requeued_not_honoured(self):
        action, payload = self._plan("@bob:ag2.space")
        self.assertEqual(action, "requeue", "a cross-sender dedup was honoured — the asker is silenced")
        self.assertEqual(payload, "task-new")
        body = (Path(self.tmp) / "ws0" / "tasks" / "task-new.txt").read_text()
        self.assertIn("DIFFERENT sender", body)


    def test_cross_sender_requeue_is_capped_like_the_other_branch(self):
        # `dedup_decision` short-circuits on holder-delivered, so its own
        # requeue cap never runs here and the fold would re-ask forever.
        first, _ = self._plan("@bob:ag2.space")
        self.assertEqual(first, "requeue")
        second, msg = self._plan("@bob:ag2.space", count=1)
        self.assertEqual(second, "report", "cross-sender re-asks without bound")
        self.assertIn("asked by someone else", msg)

    def test_holder_empty_cap_still_works(self):
        # Positive control: the branch the existing cap does guard.
        self.assertEqual(self._plan("@alice:ag2.space", holder_result="[no-send]\n")[0], "requeue")
        self.assertEqual(self._plan("@alice:ag2.space", holder_result="[no-send]\n", count=1)[0], "report")

    def test_same_room_same_sender_is_still_honoured(self):
        # The case dedup exists for; a fix that requeues this is worse than the bug.
        self.assertEqual(self._plan("@alice:ag2.space"), ("honour", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
