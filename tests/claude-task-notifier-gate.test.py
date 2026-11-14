#!/usr/bin/env python3
"""The notifier's dispatch gate: what lets a task be typed into the pane and what holds it.

Split out of claude-task-notifier.test.py by behaviour (that file keeps the harness and
the smaller suites; claude-task-notifier-submit.test.py has block escalation and submit
confirmation) so no single CI file runs for minutes. Test names are unchanged.

Run: python3 tests/claude-task-notifier-gate.test.py
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
BUSY_FOOTER = _h.BUSY_FOOTER
BUSY_STATUS = _h.BUSY_STATUS
DRAFT_FOOTER = _h.DRAFT_FOOTER
IDLE_FOOTER = _h.IDLE_FOOTER
TRUST_GATE_PANE = _h.TRUST_GATE_PANE


class EventDispatchTests(FakeTmuxHarness):
    """--event <filename>: the pane gate, self-report trust and the banners that hold a task."""

    def test_existing_result_is_never_dispatched(self):
        self.write_task("task-a.txt")
        self.write_result("task-a.txt")
        result = self.run_event("task-a.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a task with an existing result must never be typed into the pane")

    def test_empty_live_placeholder_with_no_ready_result_anywhere_is_still_dispatched(self):
        # `[ -f results/<f> ]` treated an empty file as delivered regardless
        # of content; the shared module's READY walk rejects whitespace-only.
        self.write_task("task-c.txt")
        (self.results_dir / "task-c.txt").write_text("")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-c.txt", self.sendkeys_log_text(),
                       "an empty placeholder with nothing ready behind it must not block dispatch")

    def test_pending_task_is_typed_and_submitted(self):
        self.write_task("task-b.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-b.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-b.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-b.txt", log)
        self.assertIn("ENTER", log)
        pane = self.pane_file.read_text()
        self.assertIn("Sutando task ready: task-b.txt", pane)
        self.assertIn("follow CLAUDE.md", log)

    def test_a_stale_running_self_report_does_not_block_an_idle_pane(self):
        # The status file is never read; a stale "running" is as irrelevant as a fresh one.
        self.write_task("task-stale.txt")
        self.write_status("running", ts=time.time() - 200)
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-stale.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-stale.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-stale.txt", self.sendkeys_log_text())

    def test_a_fresh_running_self_report_does_not_block_an_idle_pane(self):
        # The status file is the core's own report; a killed turn leaves it
        # "running" while the pane shows the idle prompt. The pane decides.
        self.write_task("task-fresh.txt")
        self.write_status("running", ts=time.time())
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-fresh.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-fresh.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-fresh.txt", self.sendkeys_log_text(),
                      "a self-reported 'running' must not outrank an idle pane")

    def test_a_running_turn_still_receives_the_task(self):
        # The pane shows an in-flight turn. The Monitor tool's notification
        # never waited for it, and neither does this: the line queues behind it.
        self.write_task("task-c.txt")
        self.pane_file.write_text(BUSY_FOOTER + "\n")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-c.txt", log,
                      "a running turn is not a gate; the line must be typed")
        self.assertIn("ENTER", log, "and submitted, so the CLI queues it")

    def test_trust_gate_on_stale_status_blocks_dispatch(self):
        # Pins the delegation to the REAL core-input-watch.py: a stale status
        # plus a pane stuck on the folder-trust gate must not read as idle.
        self.write_task("task-gate.txt")
        self.write_status("running", ts=time.time() - 200)
        self.pane_file.write_text(TRUST_GATE_PANE + "\n")
        result = self.run_event("task-gate.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a folder-trust gate must never be typed over")

    def test_trust_gate_on_fresh_idle_status_blocks_dispatch(self):
        # A fresh (non-stale) idle status alone must not satisfy dispatch --
        # a trust-gate pane is "not busy" too (no in-flight turn to interrupt).
        self.write_task("task-gate2.txt")
        self.write_status("idle")
        self.pane_file.write_text(TRUST_GATE_PANE + "\n")
        result = self.run_event("task-gate2.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a folder-trust gate must never be typed over, even under a fresh idle status")

    def test_stale_same_task_marker_plus_swallowed_paste_is_not_mistaken_for_staged(self):
        # A prior episode's prompt in OLDER scrollback (above the current
        # composer) plus this episode's paste swallowed must not read staged.
        self.pane_file.write_text("Sutando task ready: task-i.txt\n" + IDLE_FOOTER + "\n")
        self.write_task("task-i.txt")
        self.swallow_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-i.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-i.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        type_calls = self.sendkeys_log_text().count("TYPE Sutando task ready: task-i.txt")
        self.assertEqual(type_calls, 2,
                          "a stale marker from a prior episode must not be read as this "
                          "episode's own staged paste -- the swallowed retype must still fire")

    def test_stale_marker_plus_concurrent_owner_draft_is_not_mistaken_for_staged(self):
        # Same stale marker, but the swallowed paste is masked by an
        # UNRELATED pane change instead of leaving the tail byte-identical.
        self.pane_file.write_text("Sutando task ready: task-k.txt\n" + IDLE_FOOTER + "\n")
        self.write_task("task-k.txt")
        self.swallow_flag.write_text("1")
        self.concurrent_draft_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-k.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-k.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("TYPE Sutando task ready: task-k.txt"), 1)
        self.assertNotIn("ENTER", log,
                         "a stale marker plus an unrelated concurrent pane change must not "
                         "satisfy staging")
        # The owner's draft now occupies the composer: the retype must refuse
        # rather than type over it (failing closed beats a second paste).
        self.assertIn("composer not empty",
                      (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_composer_draft_blocks_typing(self):
        # An unsent owner draft in the composer must never be typed over,
        # even though the pane is otherwise idle-ready (no gate signature).
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-m.txt")
        result = self.run_event("task-m.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text(),
                          "an unsent owner draft in the composer must never be typed over")
        self.assertFalse((self.results_dir / "task-m.txt").exists(),
                          "a task blocked on a draft composer must stay queued, not consumed")

    def test_an_abnormal_banner_holds_the_task(self):
        # Parked on an API error: the one state a running turn is not. Hold.
        self.write_task("task-abn.txt")
        self.pane_file.write_text("API Error: 529 Overloaded\n" + IDLE_FOOTER + "\n")
        result = self.run_event("task-abn.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "an abnormal pane must not be typed into")
        self.assertIn("did not become healthy", result.stderr)

    def test_the_clis_connection_error_retry_banner_holds_the_task(self):
        # The retry family, under the CLI's own result prefix: nothing is being served.
        self.write_task("task-retry.txt")
        self.pane_file.write_text("  ⎿  Connection error. Retrying in 2 seconds…\n" + IDLE_FOOTER + "\n")
        result = self.run_event("task-retry.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "a retrying pane must not be typed into")

    def test_prose_about_a_connection_error_on_screen_does_not_hold(self):
        # The banner families are line-anchored; a transcript discussing errors is not one.
        self.write_task("task-prose.txt")
        self.pane_file.write_text("⏺ I once saw a Connection error. Retrying was the fix.\n" + IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-prose.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-prose.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose.txt", self.sendkeys_log_text())

    def test_prose_under_the_tool_result_prefix_does_not_hold(self):
        # `⎿` is also the ordinary tool-result prefix; a whole-line grammar tells the banner from it.
        self.write_task("task-prose2.txt")
        self.pane_file.write_text("  ⎿  Connection error. Retrying was the fix.\n" + IDLE_FOOTER + "\n")
        t = self._finish_on("task-prose2.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-prose2.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose2.txt", self.sendkeys_log_text())

    def test_prose_naming_an_api_error_or_a_retry_does_not_hold(self):
        for name, line in (("task-p4.txt", "⏺ API Error handling is covered by tests."),
                           ("task-p5.txt", "  ⎿  Connection error. The fix was retrying")):
            self.write_task(name)
            self.pane_file.write_text(line + "\n" + IDLE_FOOTER + "\n")
            t = self._finish_on(name, lambda log, n=name: f"TYPE Sutando task ready: {n}" in log and "ENTER" in log)
            result = self.run_event(name)
            t.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"TYPE Sutando task ready: {name}", self.sendkeys_log_text(), line)

    def test_a_wrapped_sentence_starting_with_a_retry_word_does_not_hold(self):
        self.write_task("task-prose3.txt")
        self.pane_file.write_text("⏺ I verified the docs that say\n  Connection error handling is covered by tests.\n" + IDLE_FOOTER + "\n")
        t = self._finish_on("task-prose3.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-prose3.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose3.txt", self.sendkeys_log_text())

    def _finish_on(self, name, predicate):
        import threading
        def _run():
            for _ in range(100):
                if predicate(self.sendkeys_log_text()):
                    self.write_result(name)
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_run); t.start()
        return t

    def test_the_queued_messages_composer_is_not_a_draft(self):
        # A line already queued behind the turn leaves this hint in the composer;
        # the next task must still go in, on top of the queue.
        self.write_task("task-q.txt")
        self.pane_file.write_text("❯ Press up to edit queued messages\n" + BUSY_STATUS + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-q.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-q.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-q.txt", self.sendkeys_log_text())

    def test_a_busy_footer_alone_does_not_confirm_a_submit(self):
        # Busy is trivially true once a turn runs, so it proves nothing about our
        # line: a swallowed C-m on a busy pane must still be re-pressed.
        self.write_task("task-h2.txt")
        self.swallow_enter_flag.write_text("1")
        self.busy_after_enter_flag.write_text("1")
        import threading
        def _finish():
            for _ in range(80):
                if self.sendkeys_log_text().count("ENTER") >= 2:
                    self.write_result("task-h2.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-h2.txt", timeout=15)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.sendkeys_log_text().count("ENTER"), 2,
                                "a busy footer must not stand in for the prompt leaving the composer")

    def test_no_status_file_does_not_block_an_idle_pane(self):
        # A fresh install or a core that never wrote its status has no file;
        # the pane alone shows whether a task can be typed.
        self.status_file.unlink()
        self.write_task("task-g.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-g.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-g.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-g.txt", self.sendkeys_log_text(),
                      "a missing status file must not hold a task on an idle pane")


if __name__ == "__main__":
    unittest.main()
