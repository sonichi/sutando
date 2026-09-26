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
    the limit, each read back EXACTLY before the next. The box shows only what the
    window has room for, so the window is grown for the paste and put back after;
    nothing shorter than the whole prompt is ever submitted."""

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

    def _resizes(self):
        return self.resize_log.read_text().splitlines() if self.resize_log.exists() else []

    def _sizes(self):
        return [int(l.split()[2]) for l in self._resizes()]

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

    def test_the_window_is_grown_for_the_paste_and_put_back_before_the_result_wait(self):
        self.composer_view_rows_flag.write_text("6")  # the 29-row box shows its last 6 rows
        r = self._run_long(self.LONG)
        log = self.sendkeys_log_text()
        self.assertIn("ENTER", log)
        self.assertEqual(r.returncode, 0, r.stderr)
        sizes = self._sizes()
        self.assertGreater(sizes[0], 29, sizes)
        self.assertEqual(sizes[-1], 29, sizes)
        self.assertTrue(self._resizes()[-1].endswith("enters=1"), "put back after Enter, not before: " + self._resizes()[-1])

    def test_a_box_cut_by_the_screen_bottom_is_grown_and_then_read_whole(self):
        # A 29-row pane under a tall transcript tail shows rows 2-5 of the box and pushes
        # its frame off screen; grown, the box shows every row and the frame.
        self.composer_view_rows_flag.write_text("4@1")
        r = self._run_long(self.LONG)
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_dropped_last_chunk_is_never_submitted_and_the_window_is_put_back(self):
        self.drop_paste_from_flag.write_text("5")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        log = self.sendkeys_log_text()
        self.assertNotIn("ENTER", log)
        self.assertEqual(log.count("TYPE "), 5, "kept typing past the chunk that never landed")
        self.assertIn("did not read back", r.stderr)
        self.assertEqual(self._sizes()[-1], 29, self._resizes())

    def test_a_dropped_chunk_under_a_cut_box_is_never_submitted(self):
        # The reviewer's case: cut box, pastes 5+ never land. Grown, the box shows what
        # landed, which is not the prompt.
        self.composer_view_rows_flag.write_text("4@1")
        self.drop_paste_from_flag.write_text("5")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("did not read back", r.stderr)

    def test_a_pane_that_gets_no_rows_from_the_grown_window_fails_closed(self):
        # A split window: resize-window grows the window, the composer pane keeps its rows.
        self.composer_view_rows_flag.write_text("4@1")
        self.split_pane_flag.write_text("")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("even after growing the window", r.stderr)
        self.assertEqual(self._sizes()[-1], 29, self._resizes())

    def test_a_window_that_cannot_grow_still_delivers_a_prompt_that_fits(self):
        self.grow_fails_flag.write_text("")
        name = "task-fits.txt"
        self.write_task(name)
        t = self._finish_on(name, lambda log: "ENTER" in log)
        r = self.run_event(name, timeout=40)
        t.join()
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertIn("could not grow the window", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_killed_notifier_puts_the_window_back(self):
        import signal
        import subprocess
        self.write_task(self.LONG)
        p = subprocess.Popen(["/bin/bash", str(_h.NOTIFIER), "--event", self.LONG], env=self._env({"SUTANDO_NOTIFIER_POLL_INTERVAL": "0.5"}),
                             cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if "TYPE " in self.sendkeys_log_text(): break
            time.sleep(0.1)
        self.assertIn("TYPE ", self.sendkeys_log_text(), "never started typing")
        p.send_signal(signal.SIGTERM); p.wait(timeout=10)
        self.assertEqual(self._sizes()[-1], 29, self._resizes())
        self.assertNotIn("ENTER", self.sendkeys_log_text())

    def test_a_cut_tail_found_at_pick_is_left_alone(self):
        tail = self.expected_prompt(self.LONG)[-200:]
        self.pane_file.write_text("❯ " + tail + "\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("TYPE", self.sendkeys_log_text())
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("a paste cut short", r.stderr)

    def test_a_partial_prompt_found_at_pick_under_a_cut_box_is_not_submitted(self):
        # A restart between typing and Enter: grown, the box shows a prompt missing its
        # end, which is not staged and not typed over.
        import textwrap
        rows = textwrap.wrap("❯ " + self.expected_prompt(self.LONG)[:768], 118, subsequent_indent="  ",
                             break_long_words=True, break_on_hyphens=False)
        self.composer_view_rows_flag.write_text("4@1")
        self.pane_file.write_text("\n".join(rows) + "\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertNotIn("TYPE", self.sendkeys_log_text())
        self.assertIn("composer not empty", r.stderr)

    def test_a_chunk_ending_in_a_semicolon_lands_whole(self):
        # tmux drops a trailing ';' from a send-keys argument; the chunk carries it as '\;'.
        # The name puts the first chunk's 256th byte on a ';' (the prompt opens with 20 bytes).
        name = "task-" + "x" * 230 + ";" + "y.txt"
        self.assertEqual(self.expected_prompt(name)[255], ";")
        self.write_task(name)
        t = self._finish_on(name, lambda log: "ENTER" in log)
        r = self.run_event(name, timeout=40)
        t.join()
        chunks = [l[5:] for l in self.sendkeys_log_text().splitlines() if l.startswith("TYPE ")]
        self.assertTrue(chunks[0].endswith(r"\;"), chunks[0][-10:])
        self.assertEqual("".join(c[:-2] + ";" if c.endswith(r"\;") else c for c in chunks), self.expected_prompt(name))
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
