#!/usr/bin/env python3
"""On stand-down the standby clears a prompt it staged and never got submitted,
only when every guard holds: a session watcher is ready for the inbox, the
composer holds exactly that staged prompt, the session is idle, and the task is
handled (a result, archived, or announced by the session watcher's sweep).

tmux is the fake from tests/claude-task-notifier.test.py. The session watcher is
a real process whose argv is a session-role watcher and whose sentinel names it,
so the notifier's readiness check runs unmodified.
Run: python3 tests/claude-task-notifier-standdown.test.py
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_task_notifier_harness", Path(__file__).resolve().parent / "claude-task-notifier.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
FakeTmuxHarness = _h.FakeTmuxHarness
NOTIFIER = _h.NOTIFIER
REPO = _h.REPO
IDLE_FOOTER = _h.IDLE_FOOTER
BUSY_STATUS = _h.BUSY_STATUS

DISPATCH = REPO / "src/delivery/task_dispatch.py"
IDLE_STATUS = IDLE_FOOTER.split("\n", 1)[1]


class StandDownFixtures(FakeTmuxHarness):
    """Fixtures only; the test classes below add the cases."""

    def setUp(self):
        super().setUp()
        self.staged_dir = self.state_dir / "task-notifier-staged"
        self.alerts = self.root / "osascript.calls"
        stub = self.bin / "osascript"
        stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.alerts}"\n')
        stub.chmod(0o755)

    # -- fixtures -------------------------------------------------------------

    def session_watcher(self):
        """A live session-role watcher for this inbox whose readiness sentinel names it."""
        fake = self.root / "sw" / "watch-tasks-stream.sh"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text("while :; do sleep 1; done\n")
        proc = subprocess.Popen(["/bin/bash", str(fake), str(self.tasks_dir), "--role", "session",
                                 "--inbox", str(self.tasks_dir)], start_new_session=True)
        self.addCleanup(self._stop, proc)
        (self.state_dir / "watch-tasks-stream.pid").write_text(f"{proc.pid}\n")
        return proc

    @staticmethod
    def _stop(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)

    def stage(self, name, composer=None, status=IDLE_STATUS, record=True):
        """The pane shows `composer` (default: exactly name's prompt); the standby's record says it staged the prompt."""
        self.write_task(name)
        prompt = self.expected_prompt(name)
        self.pane_file.write_text(f"❯ {prompt if composer is None else composer}\n{status}\n")
        if record:
            subprocess.run([sys.executable, str(DISPATCH), "partial-mark", str(self.staged_dir), name, "4242"],
                           input=prompt, text=True, check=True)
        return prompt

    def clear(self, timeout=15):
        return subprocess.run(["/bin/bash", str(NOTIFIER), "--clear-staged"], env=self._env(),
                              cwd=str(self.root), capture_output=True, text=True, timeout=timeout)

    def composer(self):
        rows = [r for r in self.pane_file.read_text().splitlines() if r.startswith("❯")]
        return rows[-1][1:].strip() if rows else None

    def alert_text(self, wait):
        """The backgrounded alert's recorded text, polled for up to `wait` seconds."""
        deadline = time.time() + wait
        while time.time() < deadline and not (self.alerts.exists() and self.alerts.read_text()):
            time.sleep(0.1)
        return self.alerts.read_text() if self.alerts.exists() else ""

    def assert_left(self, res, pane_before, why, alerted=True):
        self.assertNotIn("CLEAR", self.sendkeys_log_text(), "the composer was cleared")
        self.assertEqual(self.pane_file.read_text(), pane_before, "the composer changed")
        self.assertIn("leaving", res.stderr)
        self.assertIn(why, res.stderr)
        log = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertIn(why, log, "the reason was not logged")
        got = self.alert_text(wait=5 if alerted else 1)
        if alerted:
            self.assertIn("left a staged task prompt", got, "the owner was not told the standby left its prompt")
            self.assertIn("not cleared because", got)
            self.assertNotIn("blocking task delivery", got, "a stand-down is not a delivery block")
        else:
            self.assertEqual(got, "")


class StandDownClearTests(StandDownFixtures):

    # -- the happy path -------------------------------------------------------

    def test_a_prompt_the_standby_left_staged_is_cleared_once_its_task_is_answered(self):
        # Delivered for real: every Enter swallowed, so the prompt stays staged.
        self.write_task("task-ok.txt")
        self.swallow_enter_flag.write_text("1")
        first = self.run_event("task-ok.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1",
                                                        "SUTANDO_NOTIFIER_SUBMIT_RETRIES": "2"}, timeout=20)
        self.assertIn("NOT confirmed", first.stderr)
        self.assertEqual(self.composer(), self.expected_prompt("task-ok.txt"))
        self.assertTrue((self.staged_dir / "task-ok.txt").is_file(), "the standby kept no record of what it staged")
        self.session_watcher()
        self.write_result("task-ok.txt")
        res = self.clear()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CLEAR C-a C-k", self.sendkeys_log_text())
        self.assertEqual(self.composer(), "", "the composer is not empty after the clear")
        self.assertIn("composer verified empty", (self.logs_dir / "claude-task-notifier.log").read_text())
        self.assertFalse((self.staged_dir / "task-ok.txt").exists(), "the staged record outlived the clear")

    def test_a_task_the_session_watcher_announces_counts_as_taken_over(self):
        self.stage("task-sweep.txt")
        self.session_watcher()
        res = self.clear()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CLEAR C-a C-k", self.sendkeys_log_text())
        self.assertEqual(self.composer(), "")

    def test_a_submitted_prompt_leaves_no_staged_record(self):
        self.write_task("task-sent.txt")
        res = self.run_event("task-sent.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertIn("ENTER", self.sendkeys_log_text(), res.stderr)
        self.assertFalse((self.staged_dir / "task-sent.txt").exists())

    # -- each guard failing: nothing is cleared -------------------------------

    def test_owner_text_mixed_in_is_left(self):
        prompt = self.stage("task-mix.txt", composer=None)
        self.pane_file.write_text(f"❯ {prompt} and also check my calendar\n{IDLE_STATUS}\n")
        self.session_watcher()
        self.write_result("task-mix.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "with other text mixed in")

    def test_a_partial_prompt_is_left(self):
        prompt = self.expected_prompt("task-part.txt")
        self.stage("task-part.txt", composer=prompt[:120])
        self.session_watcher()
        self.write_result("task-part.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "only part of task-part.txt's prompt")

    def test_a_session_mid_turn_is_left(self):
        self.stage("task-busy.txt", status=BUSY_STATUS)
        self.session_watcher()
        self.write_result("task-busy.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "the session is not idle")

    def test_a_task_not_yet_handled_is_left(self):
        # No result, still in tasks/, and a handler claim holds it: no sweep announces it.
        self.stage("task-held.txt")
        claims = self.state_dir / "task-event-handler-claims"
        claims.mkdir(parents=True)
        (claims / "task-held.txt").write_text("claim\n")
        self.session_watcher()
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "has not taken it over")

    def test_no_session_watcher_is_left(self):
        self.stage("task-nosw.txt")
        self.write_result("task-nosw.txt")
        before = self.pane_file.read_text()
        res = self.clear()
        self.assertNotEqual(res.returncode, 0)
        self.assert_left(res, before, "no session-role watcher is ready", alerted=False)

    def test_a_clear_that_does_not_empty_the_composer_is_alerted_not_retried(self):
        self.stage("task-stuck.txt")
        self.session_watcher()
        self.write_result("task-stuck.txt")
        (self.root / "clear-ignored.flag").write_text("1")
        res = self.clear()
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.sendkeys_log_text().count("CLEAR"), 1, "the clear keystroke was retried")
        self.assertIn("not empty after clearing", res.stderr)
        self.assertIn("left a staged task prompt", self.alert_text(wait=5))
        self.assertTrue((self.staged_dir / "task-stuck.txt").exists(), "the record went with a failed clear")

    def test_a_line_break_the_owner_added_is_left_before_any_keystroke(self):
        # Whitespace-equal to the prompt, but row 1 ends where the next word would have fit.
        prompt = self.expected_prompt("task-nl.txt")
        head, tail = prompt[:30].rsplit(" ", 1)[0], None
        tail = prompt[len(head):].lstrip()
        self.stage("task-nl.txt", composer=f"{head}\n  {tail}")
        self.session_watcher()
        self.write_result("task-nl.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "line break the owner added")

    def test_a_trailing_blank_line_the_owner_added_is_left(self):
        prompt = self.expected_prompt("task-nl2.txt")
        self.stage("task-nl2.txt", composer=f"{prompt}\n  ")
        self.session_watcher()
        self.write_result("task-nl2.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "line break the owner added")

    def test_records_from_an_earlier_incarnation_are_pruned(self):
        subprocess.run([sys.executable, str(DISPATCH), "partial-mark", str(self.staged_dir), "task-old.txt", "9999"],
                       input="old prompt", text=True, check=True)
        self.stage("task-now.txt", status=BUSY_STATUS)
        self.session_watcher()
        self.clear()
        self.assertFalse((self.staged_dir / "task-old.txt").exists(), "a dead incarnation's record was kept")
        self.assertTrue((self.staged_dir / "task-now.txt").exists(), "the current incarnation's record was pruned")

    def test_an_exact_prompt_with_no_staged_record_is_left(self):
        self.stage("task-norec.txt", record=False)
        self.session_watcher()
        self.write_result("task-norec.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "did not stage", alerted=False)


class WrappedStandDownTests(StandDownFixtures):
    """C-a C-k kills one visual row of a wrapped composer (measured live: 5 rows, then 4, ...)."""
    WRAP_COLS = 60
    WRAP_STYLE = "word"

    def stage_wrapped(self, name):
        self.write_task(name)
        self.swallow_enter_flag.write_text("1")
        self.run_event(name, env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1",
                                        "SUTANDO_NOTIFIER_SUBMIT_RETRIES": "2"}, timeout=20)
        self.assertTrue((self.staged_dir / name).is_file(), "the wrapped prompt was not recorded as staged")
        return self.composer_rows()

    def composer_rows(self):
        lines = self.pane_file.read_text().splitlines()
        idx = max(i for i, r in enumerate(lines) if r.startswith("❯"))
        n = 1
        while idx + n < len(lines) and lines[idx + n].startswith("  ") and "⏵⏵" not in lines[idx + n]:
            n += 1
        return n

    def test_a_wrapped_prompt_is_cleared_one_visual_row_per_round(self):
        rows = self.stage_wrapped("task-wrap.txt")
        self.assertGreater(rows, 3, "the fixture prompt did not wrap")
        self.session_watcher()
        self.write_result("task-wrap.txt")
        res = self.clear(timeout=40)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.sendkeys_log_text().count("CLEAR C-a C-k"), rows, "one round per visual row")
        self.assertEqual(self.composer(), "")
        self.assertIn(f"after {rows} clear round(s)", (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_a_box_taller_than_the_window_is_cleared_with_the_window_grown(self):
        rows = self.stage_wrapped("task-tall.txt")
        # Ungrown, the 29-row window shows only the box's last 3 rows: the prompt reads as partial.
        self.composer_view_rows_flag.write_text("3")
        self.session_watcher()
        self.write_result("task-tall.txt")
        self.resize_log.write_text("")
        res = self.clear(timeout=40)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.composer(), "")
        self.assertEqual(self.sendkeys_log_text().count("CLEAR C-a C-k"), rows)
        resizes = [l for l in self.resize_log.read_text().splitlines() if l.startswith("RESIZE")]
        self.assertEqual(len(resizes), 2, "the window was not grown and put back")
        self.assertEqual(self.window_rows.read_text().strip(), "29", "the window was left grown")

    def test_an_empty_composer_never_grows_the_window(self):
        self.stage_wrapped("task-gone.txt")
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        self.session_watcher()
        self.resize_log.write_text("")
        res = self.clear()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("RESIZE", self.resize_log.read_text(), "the window was grown for an empty composer")

    def test_a_signal_mid_clear_puts_the_window_back(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                name = f"task-sig-{sig.name}.txt"
                self.sendkeys_log.write_text("")
                self.pane_file.write_text(IDLE_FOOTER + "\n")
                self.stage_wrapped(name)
                self.session_watcher()
                self.write_result(name)
                self.resize_log.write_text("")
                # The round changes nothing and the verify waits long: the clear is in flight at the signal.
                (self.root / "clear-ignored.flag").write_text("1")
                proc = subprocess.Popen(["/bin/bash", str(NOTIFIER), "--clear-staged"],
                                        env=self._env({"SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT": "600"}),
                                        cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.time() + 20
                while time.time() < deadline and "CLEAR" not in self.sendkeys_log_text():
                    time.sleep(0.1)
                self.assertIn("CLEAR", self.sendkeys_log_text(), "the clear never started")
                self.assertNotEqual(self.window_rows.read_text().strip(), "29", "the window was not grown")
                proc.send_signal(sig)
                proc.communicate(timeout=15)
                self.assertEqual(self.window_rows.read_text().strip(), "29", f"{sig.name} left the window grown")
                self.assertIn("WINOPT", self.resize_log.read_text(), "the window-size option was not put back")
                (self.root / "clear-ignored.flag").unlink()

    def test_owner_text_appended_mid_clear_stops_the_clear(self):
        self.stage_wrapped("task-race.txt")
        self.session_watcher()
        self.write_result("task-race.txt")
        (self.root / "owner-types-mid-clear.flag").write_text("1")
        res = self.clear(timeout=40)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.sendkeys_log_text().count("CLEAR"), 1, "a round ran over the owner's text")
        self.assertIn("OWNERTEXT", self.pane_file.read_text(), "the owner's text was killed")
        self.assertIn("text other than the rest of", res.stderr)
        self.assertIn("left a staged task prompt", self.alert_text(wait=5))


class LineBreakParseTests(unittest.TestCase):
    """pane_gate.composer_has_line_break: the parse the stand-down's newline guard asks."""

    def setUp(self):
        sys.path.insert(0, str(REPO / "src"))
        from delivery import pane_gate
        self.pg = pane_gate

    def test_a_greedy_wrap_is_not_a_line_break(self):
        import textwrap
        rows = textwrap.wrap("❯ " + " ".join(f"word{i}" for i in range(60)), width=50, subsequent_indent="  ")
        self.assertFalse(self.pg.composer_has_line_break("\n".join(rows + [IDLE_STATUS])))

    def test_a_short_row_the_next_word_fits_is_a_line_break(self):
        self.assertTrue(self.pg.composer_has_line_break(f"❯ short\n  next {'x' * 40}\n{IDLE_STATUS}"))

    def test_a_blank_row_inside_the_box_is_a_line_break(self):
        self.assertTrue(self.pg.composer_has_line_break(f"❯ one line\n  \n{IDLE_STATUS}"))

    def test_no_prompt_line_is_unknown(self):
        self.assertIsNone(self.pg.composer_has_line_break("nothing here"))


if __name__ == "__main__":
    unittest.main()
