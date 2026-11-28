#!/usr/bin/env python3
"""Dedup recovery must record origins where the READERS look, under a split layout.

`AGENT_CONNECT_TASK_DIR` and `AGENT_CONNECT_STATE_DIR` are resolved
independently (`_dirs.task_dir` / `_dirs.state_dir`), so once either is set,
`state/` is NOT a sibling of the task directory. `plan_dedup_recovery` once
derived its state dir as `tasks_dir.parent / "state"`; with the dirs split, it
wrote origins into a directory nobody reads, and the fold lost the origin
message ids it exists to preserve — silently, because appending succeeds.

This is the split-brain shape tests/state-paths-adoption.test.py was written
for, and the reason that test's allowlist entry for src/dedup_recovery.py is
now narrowed to the fallback rather than claiming a shared workspace.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dedup_recovery  # noqa: E402
import task_origins  # noqa: E402


class SplitLayout(unittest.TestCase):
    def test_an_injected_state_dir_is_where_the_readers_look(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            tasks, state = base / "elsewhere" / "tasks", base / "split" / "state"
            tasks.mkdir(parents=True)
            state.mkdir(parents=True)
            task_origins.append_origins(state, "holder-1", ["$a:server"])
            # The reader's view is the injected dir, not the task dir's sibling.
            self.assertEqual(task_origins.read_origins(state, "holder-1"), ["$a:server"])
            self.assertEqual(task_origins.read_origins(tasks.parent / "state", "holder-1"), [])

    def test_the_sibling_fallback_only_covers_the_default_layout(self):
        # Documents the fallback the allowlist entry now claims, so a future
        # change that removes it fails here rather than in production.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "tasks").mkdir()
            (base / "state").mkdir()
            task_origins.append_origins(base / "state", "holder-2", ["$b:server"])
            self.assertEqual(
                task_origins.read_origins(Path(base / "tasks").parent / "state", "holder-2"),
                ["$b:server"],
                "the default layout keeps state as a sibling of tasks/")

    def test_plan_dedup_recovery_records_origins_in_the_injected_dir(self):
        """The one that GUARDS the fix: drive the real planner on a split layout.

        The signature-only version of this test passed even with the injection
        ignored, which is why it is not the test here.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            tasks, results = base / "elsewhere" / "tasks", base / "elsewhere" / "results"
            state = base / "split" / "state"          # NOT tasks.parent / "state"
            for d in (tasks, results, state):
                d.mkdir(parents=True)
            sibling = tasks.parent / "state"          # where the old code would write
            # The accepted-holder branch demands a verified destination and sender.
            header = ("source: s-space\n"
                      "channel_id: !room:server\n"
                      "user_id: @asker:server\n")
            (tasks / "orig.txt").write_text(
                header + "source_message_id: $orig:server\ntask: do the thing\n",
                encoding="utf-8")
            (tasks / "holder.txt").write_text(
                header + "source_message_id: $held:server\ntask: do the thing\n",
                encoding="utf-8")
            (results / "holder.txt").write_text("done\n", encoding="utf-8")

            action, _ = dedup_recovery.plan_dedup_recovery(
                results, tasks, "orig", "holder", "!room:server", "reask-1",
                holder_delivery_state=lambda _h: "accepted",
                state_dir=state)

            self.assertEqual(action, "honour", "the accepted-holder branch must be reached")
            self.assertTrue(task_origins.read_origins(state, "holder"),
                            "origins must land in the INJECTED state dir")
            self.assertFalse(sibling.exists() and task_origins.read_origins(sibling, "holder"),
                             "nothing may be written to the tasks_dir sibling on a split layout")


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
