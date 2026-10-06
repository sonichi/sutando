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
        return [int(l.split()[2]) for l in self._resizes() if l.startswith("RESIZE ")]

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
        restores = [l for l in self._resizes() if l.startswith("RESIZE -y 29")]
        self.assertTrue(restores[0].endswith("enters=1"), "put back after Enter, not before: " + restores[0])
        # resize-window pinned window-size to manual; the inherited value is back afterwards.
        self.assertEqual(self._resizes()[-1], "WINOPT unset", self._resizes())
        self.assertEqual(self.window_size_opt.read_text(), "", "window-size left pinned")

    def test_a_window_local_window_size_value_is_put_back_as_it_was(self):
        self.window_size_opt.write_text("latest\n")
        r = self._run_long(self.LONG)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._resizes()[-1], "WINOPT latest", self._resizes())
        self.assertEqual(self.window_size_opt.read_text().strip(), "latest")

    def test_a_chunk_boundary_never_splits_a_character(self):
        # The name puts a two-byte character across bytes 255-256 of the prompt; the first
        # chunk stops at 255 bytes and every chunk decodes on its own.
        name = "task-" + "x" * 230 + "é" + "y.txt"
        self.assertEqual(self.expected_prompt(name).encode()[255:257], "é".encode())
        self.write_task(name)
        t = self._finish_on(name, lambda log: "ENTER" in log)
        r = self.run_event(name, timeout=40)
        t.join()
        chunks = [l[5:] for l in self.sendkeys_log_text().splitlines() if l.startswith("TYPE ")]
        self.assertEqual(len(chunks[0].encode()), 255, len(chunks[0].encode()))
        self.assertEqual("".join(chunks), self.expected_prompt(name))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_box_cut_by_the_screen_bottom_is_grown_and_then_read_whole(self):
        # A 29-row pane under a tall transcript tail shows rows 2-5 of the box and pushes
        # its frame off screen; grown, the box shows every row and the frame.
        self.composer_view_rows_flag.write_text("4@1")
        r = self._run_long(self.LONG)
        self.assertIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(r.returncode, 0, r.stderr)

    def _chunks(self):
        return [l[5:] for l in self.sendkeys_log_text().splitlines() if l.startswith("TYPE ")]

    def _boundaries(self, name):
        """Byte offsets where the notifier's chunks end: PASTE_CHUNK bytes, cut between characters."""
        out, cur = [], 0
        for ch in self.expected_prompt(name):
            n = len(ch.encode())
            if cur and cur + n > 256:
                out.append(cur); cur = 0
            cur += n
        return out + [cur]

    # Chunks that land before the drop. The prompt embeds the tasks dir and repo path, so
    # its chunk count depends on the runner; two landed chunks leave at least two to resume.
    LANDED = 2

    def _pick_with_a_dropped_chunk(self):
        """Pick 1 of the resume cases: chunks 1..LANDED land, the next never does; the retry
        within the pick resumes at that chunk and loses it again. Returns that pick's run."""
        self.assertGreaterEqual(len(self._boundaries(self.LONG)), self.LANDED + 2, self._boundaries(self.LONG))
        self.drop_paste_from_flag.write_text(str(self.LANDED + 1))
        self.write_task(self.LONG)
        r = self.run_event(self.LONG, timeout=40)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertEqual(len(self._chunks()), self.LANDED + 2, self._chunks())
        self.drop_paste_from_flag.unlink()
        return r

    def _set_composer(self, text):
        """The composer holds `text` under the idle footer, wrapped as the fake pane wraps."""
        import textwrap
        rows = textwrap.wrap("❯ " + text, self.WRAP_COLS, subsequent_indent="  ",
                             break_long_words=True, break_on_hyphens=False)
        self.pane_file.write_text("\n".join(rows) + "\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")

    def test_a_dropped_chunk_is_never_submitted_and_the_window_is_put_back(self):
        # The retry within the pick resumes at the chunk that never landed (typed twice in
        # all), never from the start, and never presses Enter on the partial.
        r = self._pick_with_a_dropped_chunk()
        chunks = self._chunks()
        self.assertEqual(chunks[-1], chunks[-2], "the resume must retype the dropped chunk, not another")
        self.assertIn("did not read back", r.stderr)
        self.assertIn("resuming it there", r.stderr)
        self.assertIn("never verifiably staged", r.stderr)
        self.assertEqual(self._sizes()[-1], 29, self._resizes())

    def test_a_later_pick_resumes_after_a_dropped_chunk_and_sends_enter_once(self):
        # The live shape: a transient drop on pick 1, then the ~30 s re-pick finds the
        # composer holding exactly the chunks that landed and finishes the paste.
        self._pick_with_a_dropped_chunk()
        before = len(self._chunks())
        t = self._finish_on(self.LONG, lambda log: "ENTER" in log)
        r = self.run_event(self.LONG, timeout=40)
        t.join()
        self.assertIn("resuming it there", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("ENTER"), 1, log)
        chunks = self._chunks()
        self.assertEqual("".join(chunks[:self.LANDED] + chunks[before:]), self.expected_prompt(self.LONG))
        self.assertEqual(len(chunks[before:]), len(self._boundaries(self.LONG)) - self.LANDED,
                         "pick 2 must type every chunk after the ones that landed")

    def test_owner_text_in_the_composer_is_still_refused_after_a_dropped_chunk(self):
        self._pick_with_a_dropped_chunk()
        # The owner cleared our partial and typed their own draft; the record alone admits nothing.
        self.pane_file.write_text(_h.DRAFT_FOOTER + "\n")
        (self.bin / "osascript").write_text("#!/bin/bash\nexit 0\n"); (self.bin / "osascript").chmod(0o755)
        before = len(self._chunks())
        r = self.run_event(self.LONG, timeout=40, env_extra={"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "1"})
        self.assertEqual(len(self._chunks()), before, "typed over the owner's draft")
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("composer not empty", r.stderr)
        blocked = [l for l in r.stderr.splitlines() if "delivery blocked" in l]
        self.assertEqual(len(blocked), 1, r.stderr)
        self.assertIn("clear the composer", blocked[0])
        self.assertNotIn("press Enter", blocked[0], "Enter would submit the composer as it is")

    def test_a_prefix_not_on_a_chunk_boundary_is_refused(self):
        self._pick_with_a_dropped_chunk()
        # The next chunk landed short: our prefix plus part of a chunk is not a resume point.
        cut = self._boundaries(self.LONG)[self.LANDED - 1] + 100
        self._set_composer(self.expected_prompt(self.LONG).encode()[:cut].decode())
        before = len(self._chunks())
        r = self.run_event(self.LONG, timeout=40)
        self.assertEqual(len(self._chunks()), before, "typed over a short-landed chunk")
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("composer not empty", r.stderr)
        self.assertNotIn("resuming", r.stderr)

    def test_a_prefix_left_in_another_core_incarnation_is_refused(self):
        self._pick_with_a_dropped_chunk()
        self.pane_pid_file.write_text("9999")
        before = len(self._chunks())
        r = self.run_event(self.LONG, timeout=40)
        self.assertEqual(len(self._chunks()), before)
        self.assertNotIn("ENTER", self.sendkeys_log_text())
        self.assertIn("composer not empty", r.stderr)

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
        self.assertEqual(self._resizes()[-1], "WINOPT unset", self._resizes())
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
