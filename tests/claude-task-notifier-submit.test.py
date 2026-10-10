#!/usr/bin/env python3
"""The notifier past the gate: composer-block escalation, its notifications, and paste/submit
confirmation retries.

Split out of claude-task-notifier.test.py by behaviour (that file keeps the harness and
the smaller suites; claude-task-notifier-gate.test.py has the dispatch gate) so no single
CI file runs for minutes. Test names are unchanged.

Run: python3 tests/claude-task-notifier-submit.test.py
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
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
DRAFT_FOOTER = _h.DRAFT_FOOTER
IDLE_FOOTER = _h.IDLE_FOOTER
NOTIFIER = _h.NOTIFIER
REPO = _h.REPO


class EventDispatchTests(FakeTmuxHarness):
    """--event <filename>: block records, alerts, and staging/submit confirmation."""

    def test_a_persistent_draft_escalates_once_then_resets(self):
        # A silent retry loop behind a stale draft reads as a dead agent; the
        # owner, who alone can clear it, must be told exactly once per episode.
        calls = self.root / "osascript.calls"
        stub = self.bin / "osascript"
        stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\n')
        stub.chmod(0o755)
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-e.txt")
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "3"}
        for attempt in range(1, 6):
            res = self.run_event("task-e.txt", env_extra=env, timeout=8)
            self.assertNotIn("No such file", res.stderr,
                             f"attempt {attempt}: a missing block record is the normal first case")
            fired = self._notifications(calls, 0 if attempt < 3 else 1)
            self.assertEqual(fired, 0 if attempt < 3 else 1,
                             f"attempt {attempt}: escalate at the 3rd refusal, never again")
        log = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertEqual(log.count("delivery blocked:"), 1)
        counter = self._block_path()
        self.assertEqual(counter.read_text().split()[0], "5")
        # An empty composer ends the episode, so the next block escalates afresh.
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        self.run_event("task-e.txt", timeout=15,
                       env_extra={**env, "SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertFalse(counter.exists(), "an empty composer must reset the block count")

    def _block_path(self, env_extra=None):
        out = subprocess.run(
            ["python3", str(REPO / "src/util_paths.py"), "composer-block-path",
             str(self.root / "workspace" / "state")],
            env=self._env(env_extra), capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())

    def test_block_counts_are_per_instance(self):
        # Pool workers share the core's workspace; one pane's draft must not
        # count toward, or reset, another pane's episode.
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-i.txt")
        envs = [{"SUTANDO_INSTANCE_ID": "w1", "SUTANDO_AGENT_ID": "@a:x"},
                {"SUTANDO_INSTANCE_ID": "w2", "SUTANDO_AGENT_ID": "@a:x"},
                {"SUTANDO_INSTANCE_ID": "w1", "SUTANDO_AGENT_ID": "@b:x"},
                {"SUTANDO_INSTANCE_ID": "w/1", "SUTANDO_AGENT_ID": "@a:x"}]
        paths = [self._block_path(e) for e in envs]
        self.assertEqual(len(set(paths)), len(envs), "each (actor, instance) needs its own record")
        state = self.root / "workspace" / "state"
        for env, path in zip(envs, paths):
            self.assertEqual(path.parent, state, "an instance id must not escape state/")
            self.run_event("task-i.txt", env_extra=env, timeout=8)
            self.assertEqual(path.read_text().split()[0], "1", env)
        self.assertFalse((state / "task-notifier-composer-block").exists())

    def test_an_unwritable_record_still_alerts_and_logs(self):
        calls = self._osascript_stub()
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-u.txt")
        self._block_path().mkdir(parents=True)
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "4"}
        res = self.run_event("task-u.txt", env_extra=env, timeout=8)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self._notifications(calls, 1), 1,
                         "a count that cannot be kept must not silence the owner alert")
        log = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertIn("could not persist composer-block count", log)

    def _incarnation(self, filename, env):
        self.run_event(filename, env_extra=env, timeout=8)
        return self._block_path().read_text().split()[1]

    def test_a_record_at_threshold_alerts_unless_already_alerted(self):
        calls = self._osascript_stub()
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-t.txt")
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "3"}
        inc = self._incarnation("task-t.txt", env)
        record = self._block_path()
        # A crash between persisting the count and alerting leaves n >= threshold unmarked.
        record.write_text(f"5 {inc} task-t.txt\n")
        self.run_event("task-t.txt", env_extra=env, timeout=8)
        self.assertEqual(self._notifications(calls, 1), 1)
        self.assertEqual(record.read_text().split(), ["6", inc, "task-t.txt", "alerted"])
        self.run_event("task-t.txt", env_extra=env, timeout=8)
        time.sleep(0.5)
        self.assertEqual(self._notifications(calls, 1), 1, "an alerted episode must not re-alert")

    def test_startup_survives_an_unremovable_block_record(self):
        # The startup reset is best-effort: under set -e a failed rm must not kill the notifier.
        self._block_path().mkdir(parents=True)
        proc = subprocess.Popen(["/bin/bash", str(NOTIFIER)], env=self._env(), cwd=str(self.root),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            time.sleep(3)
            self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

    def _notifications(self, calls, expect):
        # The notification is backgrounded, so its stub may land just after the event returns.
        for _ in range(40):
            got = calls.read_text().count("display notification") if calls.exists() else 0
            if got >= expect:
                break
            time.sleep(0.05)
        return got

    def _osascript_stub(self, body=""):
        calls = self.root / "osascript.calls"
        stub = self.bin / "osascript"
        stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\n{body}\n')
        stub.chmod(0o755)
        return calls

    def _block(self, filename, n, env):
        for _ in range(n):
            self.run_event(filename, env_extra=env, timeout=8)

    def test_a_new_episode_escalates_again_without_an_empty_read(self):
        # An episode can end with no empty-composer observation (the task is answered
        # another way, or the core restarts); the next one must still reach the owner.
        calls = self._osascript_stub()
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "2"}
        self.write_task("task-x.txt")
        self._block("task-x.txt", 3, env)
        self.assertEqual(self._notifications(calls, 1), 1)
        self.write_task("task-y.txt")
        self._block("task-y.txt", 2, env)
        self.assertEqual(self._notifications(calls, 2), 2, "a block on a different task is a new episode")
        self.pane_pid_file.write_text("5151")
        self._block("task-y.txt", 2, env)
        self.assertEqual(self._notifications(calls, 3), 3, "a restarted core is a new episode")

    def test_a_hung_notification_does_not_stall_delivery(self):
        calls = self._osascript_stub("exec sleep 30")
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-h.txt")
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "1"}
        started = time.monotonic()
        result = self.run_event("task-h.txt", env_extra=env, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn("delivery blocked:", (self.logs_dir / "claude-task-notifier.log").read_text())
        # The backgrounded stub writes into the temp dir; wait for it so cleanup cannot race it.
        self.assertEqual(self._notifications(calls, 1), 1)

    def test_a_notification_ignoring_term_is_killed(self):
        pidfile = self.root / "osascript.pid"
        calls = self._osascript_stub(f'trap "" TERM\necho $$ > "{pidfile}"\nexec sleep 30')
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-k2.txt")
        env = {"SUTANDO_NOTIFIER_COMPOSER_BLOCK_ESCALATE_AFTER": "1"}
        self.run_event("task-k2.txt", env_extra=env, timeout=8)
        self.assertEqual(self._notifications(calls, 1), 1)
        for _ in range(40):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("an osascript that ignores TERM must still be killed")

    def test_ghost_text_suggestion_is_not_a_draft(self):
        # The CLI's suggested reply is dim ghost text in the EMPTY composer; a plain
        # capture shows it as typed, and every re-pick would stall on it.
        self.ghost_file.write_text("yes")
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
        t.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("composer not empty", result.stderr,
                         "ghost text must not read as an unsent draft")
        self.assertIn("TYPE", self.sendkeys_log_text(),
                      "the paste must proceed over ghost text")
        self.assertIn("ENTER", self.sendkeys_log_text(),
                      "the prompt must stage and submit once the ghost text is gone")

    def test_pane_change_after_enter_blocks_a_second_press(self):
        # After the first C-m, the pane changing to something other than
        # busy (e.g. the owner typing) must never get a second C-m.
        self.write_task("task-j.txt")
        self.owner_types_after_enter_flag.write_text("1")
        result = self.run_event("task-j.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text().count("ENTER"), 1,
                          "a pane that changed to something other than our own prompt "
                          "must not receive a second C-m")

    def test_dropped_paste_is_retyped(self):
        self.write_task("task-d.txt")
        self.swallow_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-d.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-d.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        type_calls = self.sendkeys_log_text().count("TYPE Sutando task ready: task-d.txt")
        self.assertEqual(type_calls, 2,
                          "a paste that never staged must be retyped exactly once more")

    def test_owner_text_interleaved_with_our_paste_is_not_mistaken_for_staged(self):
        # Owner text alongside our marker used to satisfy a substring check.
        # Retyping never clears the composer, so a real mix can't self-heal.
        self.write_task("task-o.txt")
        self.interleaved_owner_flag.write_text("1")
        result = self.run_event("task-o.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("TYPE Sutando task ready: task-o.txt"), 1,
                          "the mix now occupies the composer; a retype would paste over owner text")
        self.assertNotIn("ENTER", log,
                          "Enter must never fire on a composer mixing our prompt with owner text")
        self.assertIn("composer not empty",
                      (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_never_staged_returns_fast_instead_of_waiting_the_full_timeout(self):
        # Enter never sent -> give up immediately, never wait out
        # COMPLETION_TIMEOUT (8s here) for a result that can't ever appear.
        self.write_task("task-n.txt")
        self.swallow_always_flag.write_text("1")
        started = time.time()
        result = self.run_event("task-n.txt", timeout=15)
        elapsed = time.time() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ENTER", self.sendkeys_log_text(),
                          "Enter must never fire when staging never succeeded")
        # Below the 8s COMPLETION_TIMEOUT with room for the bounded, locked key sends.
        self.assertLess(elapsed, 6,
                         f"took {elapsed:.1f}s -- a never-submitted prompt must not wait out "
                         "the completion timeout")

    def test_unconfirmed_submit_is_re_pressed(self):
        # A swallowed C-m leaves the prompt staged in the composer, so
        # deliver_prompt must re-press at least once after the confirm timeout.
        self.swallow_enter_flag.write_text("1")
        self.write_task("task-e.txt")

        import threading
        def _finish():
            for _ in range(80):
                if self.sendkeys_log_text().count("ENTER") >= 2:
                    self.write_result("task-e.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-e.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.sendkeys_log_text().count("ENTER"), 2,
                                 "an unconfirmed submit must be re-pressed")

    def test_no_session_drops_without_hanging(self):
        self.session_flag.unlink()
        self.write_task("task-f.txt")
        result = self.run_event("task-f.txt", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "")

    def test_submit_confirms_when_the_prompt_leaves_the_composer(self):
        # The submitted text stays in scrollback; what confirms is the fresh
        # empty composer under it, not the pane going busy.
        self.write_task("task-h.txt")
        self.busy_after_enter_flag.write_text("1")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-h.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-h.txt", timeout=15)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("ENTER"), 1,
                          "a prompt that left the composer is confirmed on the first attempt")
        self.assertIn("Sutando task ready: task-h.txt", self.pane_file.read_text(),
                       "the submitted text staying in scrollback is the exact case this pins")

    def test_a_novel_prompt_under_an_old_idle_footer_is_not_typed_into(self):
        # An unforeseen confirmation shares the window with a stale idle footer and
        # a blank bottom composer; the pane read alone must refuse.
        self.status_file.unlink()
        self.write_task("task-novel.txt")
        self.pane_file.write_text("\n".join([
            "❯", "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
            "Overwrite the existing config file?", "Enter to confirm · Esc to cancel", "❯", ""]))
        result = self.run_event("task-novel.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "a live prompt must never receive the task as its answer")


if __name__ == "__main__":
    unittest.main()
