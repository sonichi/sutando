#!/usr/bin/env python3
"""Expected-loop policy cannot be satisfied by a worker or unrelated success."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cron_loop_policy import main_loop_problem


class LoopPolicyTest(unittest.TestCase):
    def loop(self, **changes):
        return {"name": "main-loop", "prompt_skill": "proactive-loop",
                "cron": "*/5 * * * *", **changes}

    def test_codex_requires_one_core_loop(self):
        for entries in ([], [{"name": "hourly"}], [self.loop(owner="worker-a")],
                        [self.loop(), self.loop()], [self.loop(launchd=True)]):
            with self.subTest(entries=entries):
                self.assertIn("main-loop", main_loop_problem(entries, "codex"))
        self.assertIsNone(main_loop_problem([self.loop()], "codex"))

    def test_only_deliberate_impossible_parking_is_exempt(self):
        self.assertIsNone(main_loop_problem([self.loop(disabled=True, cron="0 0 31 2 *")], "codex"))
        self.assertIsNotNone(main_loop_problem([self.loop(cron="0 0 31 2 *")], "codex"))
        self.assertIsNone(main_loop_problem([self.loop(disabled=True)], "codex"))
        self.assertIsNone(main_loop_problem([self.loop(disabled=True, cron="0 0 31 2 1")], "codex"))

    def test_invalid_schedule_or_owner_is_not_healthy(self):
        for changes in ({"cron": ""}, {"cron": "90 * * * *"}, {"execution": "unknown"}):
            with self.subTest(changes=changes):
                self.assertIsNotNone(main_loop_problem([self.loop(**changes)], "codex"))

    def test_runtime_switch_leaves_claude_registration_to_its_probe(self):
        self.assertIsNone(main_loop_problem([], "claude"))


if __name__ == "__main__":
    unittest.main()
