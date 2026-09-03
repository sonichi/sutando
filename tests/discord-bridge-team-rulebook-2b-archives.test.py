#!/usr/bin/env python3
"""Block 2b (MESSAGE OWNER) of the team-tier rulebook must archive the task.

Measured 2026-09-03 on a live host: a team-tier ask was routed to the owner
exactly as 2b said — proactive file written, no task result — and the task
then sat in tasks/ for 21 minutes, health-check's task-queue probe warned
"undrained past 900s", and the end-of-pass queue checker (#3732) reported it
UNANSWERED. The rulebook's own instruction produced the queue defect the rest
of the system treats as a miss. `[no-send]` is the marker that delivers
nothing AND archives, so 2b must say to write it.

Run: python3 tests/discord-bridge-team-rulebook-2b-archives.test.py
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py").read_text()


def block_2b(text: str) -> str:
    start = text.index("2b. MESSAGE OWNER")
    end = text.index("3. NO-REPLY", start)
    return text[start:end]


class Block2bArchives(unittest.TestCase):
    def test_2b_instructs_a_no_send_result_so_the_task_archives(self):
        b = block_2b(SRC)
        self.assertIn("[no-send]", b)
        self.assertIn("results/task-{id}.txt", b)

    def test_2b_no_longer_forbids_the_task_result(self):
        b = block_2b(SRC)
        self.assertNotIn("Do NOT write to results/task-{id}.txt", b)

    def test_2b_still_routes_the_content_to_the_owner(self):
        b = block_2b(SRC)
        self.assertIn("results/proactive-{ts}.txt", b)

    def test_positive_control_the_old_wording_would_fail(self):
        old = SRC.replace("Then write exactly `[no-send]` to results/task-{id}.txt", "Do NOT write to results/task-{id}.txt (no sender reply)")
        b = block_2b(old)
        self.assertIn("Do NOT write to results/task-{id}.txt", b)
        self.assertNotIn("[no-send]", b.split("Do NOT")[0])


if __name__ == "__main__":
    unittest.main()
