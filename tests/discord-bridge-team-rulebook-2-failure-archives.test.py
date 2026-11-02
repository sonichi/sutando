#!/usr/bin/env python3
"""Block 2's FAILURE fallback must archive the task, exactly as 2b now does.

#3775 fixed block 2b — the owner-ping path left a task with no result, so it
sat in tasks/ forever and both health-check's task-queue probe and the
end-of-pass queue check reported it unanswered. The identical instruction
survived one branch up, in block 2's FAILURE fallback: `grep -c` for the
forbidding wording returned 2 on the pre-#3775 tree and 1 after.

That branch fires more often than 2b in one respect: 2b needs a human-judgement
case, while this one triggers on stall (125), cap (124), or any gh/codex error.

Run: python3 tests/discord-bridge-team-rulebook-2-failure-archives.test.py
"""
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py").read_text()


ANCHOR = ("then write exactly `[no-send]` to results/task-{id}.txt "
          "so the task archives")
REVERTED = "and do NOT write results/task-{id}.txt"


def block_2(text: str) -> str:
    start = text.index("2. PR-REVIEW REQUEST")
    end = text.index("2b. MESSAGE OWNER", start)
    return text[start:end]


class Block2FailureArchives(unittest.TestCase):
    def test_the_failure_fallback_instructs_a_no_send_result(self):
        b = block_2(SRC)
        self.assertIn("On FAILURE", b)
        self.assertIn("[no-send]", b)
        self.assertIn("results/task-{id}.txt", b)

    def test_the_failure_fallback_no_longer_forbids_the_task_result(self):
        self.assertNotIn("do NOT write results/task-{id}.txt", block_2(SRC))

    def test_the_failure_fallback_still_pings_the_owner(self):
        self.assertIn("results/proactive-{ts}.txt", block_2(SRC))

    def test_NEITHER_branch_forbids_the_task_result_anywhere_in_the_rulebook(self):
        """The whole point: 2b alone left the sibling live. Assert on the file,
        so a third branch added later with the old wording fails here too."""
        self.assertNotIn("NOT write results/task-{id}", SRC)
        self.assertNotIn("Do NOT write to results/task-{id}", SRC)

    def test_the_replacement_ANCHOR_still_exists(self):
        """A fixture guard, NOT a detector control. If the anchor drifts out of
        SRC, `replace` no-ops and the revert test below silently checks the
        unmutated file while still passing."""
        self.assertIn(ANCHOR, SRC)

    def test_the_BLOCK_scoped_detector_flips_on_an_in_block_revert(self):
        """Exercises the block-scoped predicate only, and says so.

        An in-block revert CANNOT exercise the file-scoped detector: its needle
        is a SUBSTRING of this one over a SUPERSET haystack, so asserting it
        here passes by implication whenever this line passes, and never runs
        when this line fails. That inert assertion lived here until
        @yixuan-ag2 proved it in review; the arm that does the job is below.
        """
        old = SRC.replace(ANCHOR, REVERTED)
        self.assertNotEqual(old, SRC, "anchor drifted: nothing was mutated")
        self.assertIn("do NOT write results/task-{id}.txt", block_2(old))

    def test_the_FILE_scoped_detector_flips_on_a_revert_OUTSIDE_block_2(self):
        """The missing arm: put the old wording where `block_2()` cannot see it.

        `test_NEITHER_branch...` exists to catch a THIRD branch added later
        carrying the forbidding wording. Only a revert outside block 2 leaves
        the block-scoped predicate blind while the file-scoped one fires, so
        only this shows the file detector is sensitive at all.

        It asserts TWO independent needles absent, and each needs its own arm:
        neither is a substring of the other, so an arm for one says nothing
        about the other. The second ("Do NOT write to ...") is the pre-#3775
        block-2b wording — real text that could reappear in block 2 or a later
        branch, which is exactly what that detector generalises over.

        It was UNARMED, not unguarded — @yixuan-ag2's correction, and the
        distinction is the one this file is about. `test_NEITHER_branch...`
        has asserted both needles since long before either arm existed, so a
        P2 regression would have failed at the previous head and every head
        before it. What was missing was any demonstration that the assertion
        could flip. A guard detects; an arm proves the guard detects.
        """
        for needle in ("NOT write results/task-{id}",
                       "Do NOT write to results/task-{id}"):
            with self.subTest(needle=needle):
                outside = SRC + f"\n# 2c. A LATER BRANCH: {needle}.txt\n"
                self.assertNotIn(needle, block_2(outside))
                self.assertIn(needle, outside)


if __name__ == "__main__":
    unittest.main()
