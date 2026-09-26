#!/usr/bin/env python3
"""The Claude task notifier's long-prompt chunked-paste cases.

Split from tests/claude-task-notifier.test.py so each file stays under the CI
per-file cap; the fake tmux harness and its constants are that file's.
Run: python3 tests/claude-task-notifier-longprompt.test.py
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_task_notifier_harness", Path(__file__).resolve().parent / "claude-task-notifier.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
# Only the harness and its constants: the TestCase classes with tests stay
# collected from their own file.
FakeTmuxHarness = _h.FakeTmuxHarness
IDLE_FOOTER = _h.IDLE_FOOTER


class LongPromptTests(FakeTmuxHarness):
    """One tmux write past the CLI's input limit lands as only its tail (measured on
    the live build at 1022 bytes: the composer held the last 42 chars of a 1064-char
    prompt, and Enter would have submitted them). A prompt is typed in chunks below
    the limit, each read back before the next; the box shows at most its last rows,
    so a tall prompt is verified through that window; a cut tail is never submitted."""

    WRAP_COLS = 118
    WRAP_STYLE = "word"
    LONG = "task-" + "l" * 200 + ".txt"

    def _finish_on(self, name, predicate):
        import threading
        def _run():
            for _ in range(100):
                if predicate(self.sendkeys_log_text()):
                    self.write_result(name); return
                time.sleep(0.1)
        th = threading.Thread(target=_run); th.start()
        return th

    def _run_long(self, name, env=None):
        self.write_task(name)
        t = self._finish_on(name, lambda log: "ENTER" in log)
        r = self.run_event(name, env, timeout=40)
        t.join()
        return r

    def test_a_prompt_past_the_cut_is_typed_in_chunks_that_reassemble_to_it(self):
        self.cut_paste_over_flag.write_text("1022")
        r = self._run_long(self.LONG)
        chunks = [l[5:] for l in self.sendkeys_log_text().splitlines() if l.startswith("TYPE ")]
        self.assertGreater(len(chunks), 2, chunks)
        self.assertTrue(all(len(c) <= 256 for c in chunks), [len(c) for c in chunks])
        self.assertEqual("".join(chunks), self.expected_prompt(self.LONG))
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_chunk_that_lands_short_is_never_submitted_or_typed_over(self):
        self.cut_paste_over_flag.write_text("100")  # a limit below even one chunk
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        log = self.sendkeys_log_text()
        self.assertNotIn("ENTER", log)
        self.assertEqual(log.count("TYPE "), 1, "typed over what landed")
        self.assertIn("composer not empty", r.stderr)

    def test_a_prompt_taller_than_the_box_is_verified_through_its_window(self):
        self.composer_view_rows_flag.write_text("6")
        r = self._run_long(self.LONG)
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertNotIn("never verifiably staged", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_box_narrower_than_a_chunk_fails_closed(self):
        self.composer_view_rows_flag.write_text("2")  # under one chunk visible
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(self.sendkeys_log_text().count("TYPE "), 1, "typed over what landed")
        self.assertIn("composer not empty", r.stderr)

    def test_a_cut_tail_found_at_pick_is_left_alone(self):
        tail = self.expected_prompt(self.LONG)[-200:]
        self.pane_file.write_text("❯ " + tail + "\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("TYPE", self.sendkeys_log_text())
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("a paste cut short", r.stderr)


if __name__ == "__main__":
    unittest.main()
