#!/usr/bin/env python3
"""The Claude task notifier's in-flight record, re-pick, narrow-pane and main-loop cases.

Split from tests/claude-task-notifier.test.py so each file stays under the CI
per-file cap; the fake tmux harness and its constants are that file's.
Run: python3 tests/claude-task-notifier-inflight.test.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
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
NOTIFIER = _h.NOTIFIER
REPO = _h.REPO
IDLE_FOOTER = _h.IDLE_FOOTER
BUSY_FOOTER = _h.BUSY_FOOTER
BUSY_STATUS = _h.BUSY_STATUS
DRAFT_FOOTER = _h.DRAFT_FOOTER
TRUST_GATE_PANE = _h.TRUST_GATE_PANE


class NarrowCaptureTests(FakeTmuxHarness):
    """A real pane soft-wraps a long row; capture-pane without -J returns the pieces,
    and a banner cut in two matches no whole-line grammar. -J is what makes it one line."""
    CAPTURE_COLS = 40

    def test_a_long_retry_banner_wrapped_by_the_pane_still_holds(self):
        self.write_task("task-wide.txt")
        self.pane_file.write_text('  ⎿  API Error (529 {"type":"overloaded_error"}) · Retrying in 1 seconds… (attempt 1/10)\n' + IDLE_FOOTER + "\n")
        result = self.run_event("task-wide.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "", "a wrapped live banner was typed over")
        self.assertIn("did not become healthy", result.stderr)

    def test_a_long_prose_row_wrapped_by_the_pane_still_delivers(self):
        self.write_task("task-wide2.txt")
        self.pane_file.write_text("⏺ I verified the docs that say Connection error handling is covered by tests and nothing more.\n" + IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(60):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-wide2.txt"); return
                time.sleep(0.1)
        t = threading.Thread(target=_finish); t.start()
        result = self.run_event("task-wide2.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-wide2.txt", self.sendkeys_log_text())


class RePickTests(FakeTmuxHarness):
    def test_a_delivered_prompt_still_in_the_pane_is_not_typed_again(self):
        # First pass types and submits; no result ever appears. The re-pick after
        # the completion timeout must see the line in the pane and wait, not queue it twice.
        self.write_task("task-dup.txt")
        first = self.run_event("task-dup.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 1)
        second = self.run_event("task-dup.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 1,
                         "the same task was typed a second time while its line was still in the pane")
        self.assertIn("awaiting its result", second.stderr)
        self.assertTrue((self.inflight_dir / "task-dup.txt").is_file(), "no in-flight marker after a confirmed submit")

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

    def test_a_staged_but_unsent_prompt_is_resumed_at_the_enter_not_retyped(self):
        # Every C-m swallowed: the prompt stays in the composer, no marker. The next
        # pick must press Enter on it, not wait on a submit that never happened.
        self.write_task("task-swal.txt")
        self.swallow_enter_flag.write_text("1")
        first = self.run_event("task-swal.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1",
                                                          "SUTANDO_NOTIFIER_SUBMIT_RETRIES": "2"}, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("NOT confirmed", first.stderr)
        # The marker precedes the Enter by design; beside a still-staged prompt it means resume.
        self.assertTrue((self.inflight_dir / "task-swal.txt").is_file(), "the submit attempt left no marker")
        enters = self.sendkeys_log_text().count("ENTER")
        self.swallow_enter_flag.unlink()
        t = self._finish_on("task-swal.txt", lambda log: log.count("ENTER") > enters)
        second = self.run_event("task-swal.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "5"}, timeout=20)
        t.join(timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("staged but unsent; resuming", second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 1, "a staged prompt was typed a second time")
        self.assertGreater(self.sendkeys_log_text().count("ENTER"), enters, "the resume never pressed Enter")

    def test_a_restart_between_the_paste_and_the_enter_resumes_at_the_enter(self):
        # The composer already holds exactly our prompt and nothing was ever pressed.
        self.write_task("task-mid.txt")
        prompt = self.expected_prompt("task-mid.txt")
        self.pane_file.write_text(f"❯ {prompt}\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        t = self._finish_on("task-mid.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-mid.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text(), "typed over a composer that already held the prompt")
        self.assertEqual(self.sendkeys_log_text().count("ENTER"), 1)

    def test_a_prompt_evicted_from_a_tiny_history_is_still_not_typed_again(self):
        # Terminal history is lossy; the marker is the record. Runs under a 2-row
        # history so the submitted prompt is gone from every capture by the re-pick.
        self.write_task("task-evict.txt")
        first = self.run_event("task-evict.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 1)
        self.pane_file.write_text("\n".join(f"⏺ output row {i}" for i in range(40)) + "\n" + IDLE_FOOTER + "\n")
        second = self.run_event("task-evict.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 1,
                         "the prompt was typed again after history evicted it")
        self.assertIn("already submitted to this core", second.stderr)

    def test_a_marker_from_a_previous_core_incarnation_does_not_hold_the_task(self):
        # The core restarted: its turn died with it, so the marker is stale and the task goes in.
        self.write_task("task-inc.txt")
        first = self.run_event("task-inc.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.pane_pid_file.write_text("9999\n")
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        t = self._finish_on("task-inc.txt", lambda log: log.count("ENTER") >= 2)
        second = self.run_event("task-inc.txt")
        t.join(timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 2, "a stale marker held the task after a core restart")

    def test_a_staged_prompt_two_pending_tasks_could_own_is_not_resumed(self):
        # `task-a b.txt` sits typed-unsent while `task-ab.txt` is also pending: the
        # composer cannot say whose line it is, so neither pick presses Enter.
        self.write_task("task-a b.txt")
        self.write_task("task-ab.txt")
        prompt = self.expected_prompt("task-a b.txt")
        self.pane_file.write_text(f"❯ {prompt}\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        result = self.run_event("task-ab.txt", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "", "an ambiguous staged prompt was submitted")
        self.assertIn("another pending task", result.stderr)

    def test_the_marker_exists_before_the_enter_is_pressed(self):
        # No crash window: a submit that reached the pane is already on record.
        self.write_task("task-ord.txt")
        t = self._finish_on("task-ord.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-ord.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        enters = [ln for ln in self.sendkeys_log_text().splitlines() if ln.startswith("ENTER")]
        self.assertTrue(enters and all(ln == "ENTER markers=1" for ln in enters), enters)

    def test_the_marker_records_the_incarnation_the_prompt_was_typed_into(self):
        # The pane pid flips right after the Enter (a restart racing the submit): the
        # marker must name the OLD core, and the next pick under the new one delivers again.
        self.write_task("task-race.txt")
        self.pid_after_enter_flag.write_text("9999\n")
        first = self.run_event("task-race.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((self.inflight_dir / "task-race.txt").read_text().strip(), "4242",
                         "the marker named the core that appeared after the Enter")
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        t = self._finish_on("task-race.txt", lambda log: log.count("ENTER") >= 2)
        second = self.run_event("task-race.txt")
        t.join(timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 2, "the new core never received the task")

    def test_an_unreadable_incarnation_refuses_to_submit(self):
        # Empty identity would read as live forever; the paste never happens.
        self.write_task("task-noid.txt")
        self.pane_pid_file.write_text("")
        result = self.run_event("task-noid.txt", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "", "typed with no incarnation to record")
        self.assertIn("incarnation unreadable", result.stderr)
        self.assertFalse(self.inflight_dir.exists() and any(self.inflight_dir.iterdir()))

    def test_a_marker_beside_a_staged_prompt_means_resume_not_await(self):
        # The crash landed after the marker and before the Enter.
        self.write_task("task-mk.txt")
        prompt = self.expected_prompt("task-mk.txt")
        self.inflight_dir.mkdir(parents=True, exist_ok=True)
        (self.inflight_dir / "task-mk.txt").write_text("4242\n")
        self.pane_file.write_text(f"❯ {prompt}\n" + IDLE_FOOTER.split("\n", 1)[1] + "\n")
        t = self._finish_on("task-mk.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-mk.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text())
        self.assertEqual(self.sendkeys_log_text().count("ENTER"), 1, "a marker beside a staged prompt was awaited, not resumed")

    def test_an_unreadable_marker_holds_the_task_rather_than_reading_as_not_in_flight(self):
        import os as _os
        if _os.geteuid() == 0:
            self.skipTest("root cannot be denied a read")
        self.write_task("task-unr.txt")
        self.inflight_dir.mkdir(parents=True, exist_ok=True)
        marker = self.inflight_dir / "task-unr.txt"
        marker.write_text("4242\n"); marker.chmod(0o000)
        try:
            result = self.run_event("task-unr.txt", timeout=10)
        finally:
            marker.chmod(0o644)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "", "an unreadable marker was read as 'not in flight' and the task typed")
        self.assertIn("cannot decide", result.stderr)

    def test_a_result_that_lands_during_the_healthy_wait_is_not_delivered(self):
        # Parked on a banner; meanwhile another path answers the task; then the pane clears.
        self.write_task("task-late.txt")
        self.pane_file.write_text("API Error: 529 overloaded\n" + IDLE_FOOTER + "\n")
        import threading
        def _answer_then_clear():
            time.sleep(1.0)
            self.write_result("task-late.txt")
            time.sleep(0.3)
            self.pane_file.write_text(IDLE_FOOTER + "\n")
        t = threading.Thread(target=_answer_then_clear); t.start()
        result = self.run_event("task-late.txt", timeout=12)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "", "a task answered during the wait was delivered again")
        self.assertIn("appeared while waiting", result.stderr)

    def test_a_failed_capture_after_the_enter_is_not_a_confirmation(self):
        # Enter swallowed and the very next capture fails: the notifier must keep
        # checking and re-press, not read the empty capture as "prompt gone".
        self.write_task("task-cap.txt")
        self.swallow_enter_flag.write_text("1")
        self.fail_capture_after_enter_flag.write_text("1")
        result = self.run_event("task-cap.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1",
                                                          "SUTANDO_NOTIFIER_SUBMIT_RETRIES": "2"}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.sendkeys_log_text().count("ENTER"), 2,
                                "a failed capture was taken as the prompt having left the composer")
        self.assertIn("NOT confirmed", result.stderr)

    def test_filenames_differing_only_in_whitespace_are_distinct_tasks(self):
        # `task-a b.txt` completing must not suppress `task-ab.txt`: identity is the
        # marker's filename, never whitespace-stripped pane text.
        self.write_task("task-a b.txt")
        t = self._finish_on("task-a b.txt", lambda log: "ENTER" in log)
        first = self.run_event("task-a b.txt")
        t.join(timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.write_task("task-ab.txt")
        t = self._finish_on("task-ab.txt", lambda log: log.count("ENTER") >= 2)
        second = self.run_event("task-ab.txt")
        t.join(timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sendkeys_log_text().count("TYPE"), 2, "the second task was taken for the first")


class MainLoopWiringTest(FakeTmuxHarness):
    """Proves the actual claim: a task file dropped on disk reaches the
    notifier via watch-tasks-stream.sh's real fswatch pipeline, unaided by
    --event. Everything else about dispatch is already covered above."""

    def _wait_for_fswatch_attach(self, timeout=10):
        # A fixed sleep guesses how long fswatch takes to attach; under load
        # that guess can be too short and flakes a real bug-free run.
        needle = str(self.tasks_dir)
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = subprocess.run(
                ["ps", "-axo", "command"], capture_output=True, text=True
            ).stdout
            if any("fswatch" in line and needle in line for line in out.splitlines()):
                return True
            time.sleep(0.1)
        return False

    def tearDown(self):
        """Every test in this class starts the real main loop, whose standby
        watcher calls setsid() and so survives a killpg of the notifier."""
        left = self._kill_strays()
        super().tearDown()
        self.assertEqual(left, [], "a fixture watcher or fswatch outlived the test")

    def _strays(self):
        """Pids still naming this fixture. `pgrep -f` matches the full argv;
        macOS `ps -o command=` truncates it to the terminal width and would
        silently report none."""
        out = subprocess.run(["pgrep", "-f", Path(self.root).name],
                             capture_output=True, text=True).stdout
        me = os.getpid()
        return [int(x) for x in out.split() if x.isdigit() and int(x) != me]

    def _kill_strays(self, grace=5.0):
        """Reap anything still naming this fixture, by process group."""
        deadline = time.time() + grace
        while time.time() < deadline:
            pids = self._strays()
            if not pids:
                return []
            for pid in pids:
                for killer in (os.killpg, os.kill):
                    try:
                        killer(pid, signal.SIGTERM)
                        break
                    except (ProcessLookupError, PermissionError):
                        continue
            time.sleep(0.3)
        for pid in self._strays():
            for killer in (os.killpg, os.kill):
                try:
                    killer(pid, signal.SIGKILL)
                    break
                except (ProcessLookupError, PermissionError):
                    continue
        time.sleep(0.3)
        return self._strays()

    def test_dropped_task_file_is_picked_up_by_the_real_watcher(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env(),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-live.txt")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-live.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("main loop never dispatched the dropped task file:\n"
                          + self.sendkeys_log_text())
            self.write_result("task-live.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_an_abnormal_core_keeps_the_notifier_alive_until_the_session_is_gone(self):
        # A pending task on a core parked on an error past the ready timeout is
        # a wait, not a death: only a vanished session ends the process.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        self.pane_file.write_text("API Error: 529 Overloaded\n" + IDLE_FOOTER + "\n")
        self.write_task("task-busy.txt")
        err = open(self.root / "notifier.stderr", "w")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env({"SUTANDO_NOTIFIER_CORE_READY_TIMEOUT": "1",
                           "SUTANDO_NOTIFIER_RETRY_POLL_SEC": "1"}),
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=err,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 6
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
            log = (self.root / "notifier.stderr").read_text()
            self.assertIn("core did not become healthy within 1s", log)
            self.assertIsNone(proc.poll(),
                              "an abnormal core made the notifier exit instead of waiting:\n" + log)
            self.assertNotIn("TYPE", self.sendkeys_log_text(),
                             "nothing may be typed into a core parked on an error")
            self.session_flag.unlink()
            deadline = time.time() + 6
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
            self.assertEqual(proc.poll(), 1,
                             "a vanished session must still end the notifier with status 1")
        finally:
            err.close()
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_a_queued_task_is_retried_with_no_further_wake_at_all(self):
        # A task left queued at its only wake has no other trigger once no
        # unrelated task arrives -- only the periodic self-poll can retry it.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env({"SUTANDO_NOTIFIER_RETRY_POLL_SEC": "1"}),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-p.txt")
            # Let the (failing) first wake pass, then clear the draft -- no
            # new task file is EVER written from here on.
            time.sleep(1.5)
            self.assertNotIn("TYPE", self.sendkeys_log_text(),
                              "a busy composer must not have been typed over")
            self.pane_file.write_text(IDLE_FOOTER + "\n")
            deadline = time.time() + 10
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-p.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("the periodic self-poll never retried the queued task:\n"
                          + self.sendkeys_log_text())
            self.write_result("task-p.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_claimed_task_is_never_selected_by_an_unrelated_wake(self):
        # next_pending_task must skip a task claimed must-handle, whichever
        # unrelated task's wake triggered the rescan -- see CLAIMS_DIR.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        claims_dir = self.state_dir / "task-event-handler-claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        self.write_task("task-claimed.txt")
        (claims_dir / "task-claimed.txt").write_text("claimed\n")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env(),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-unrelated.txt")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-unrelated.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("main loop never dispatched the unrelated task file:\n"
                          + self.sendkeys_log_text())
            self.assertNotIn(
                "Sutando task ready: task-claimed.txt", self.sendkeys_log_text(),
                "a claimed must-handle task must never be typed into the live core")
            self.write_result("task-unrelated.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_worker_held_task_is_never_typed_into_the_core(self):
        # A router hand-off sentinel under deliveries/<worker>/ leaves the file in
        # tasks/; the pick must skip it whichever unrelated task woke the scan.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        held = self.tasks_dir.parent / "deliveries" / "worker-1"
        held.mkdir(parents=True, exist_ok=True)
        self.write_task("task-held.txt")
        (held / "task-held.claimed").write_text("")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env(),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-unrelated.txt")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-unrelated.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("main loop never dispatched the unrelated task file:\n"
                          + self.sendkeys_log_text())
            self.assertNotIn(
                "Sutando task ready: task-held.txt", self.sendkeys_log_text(),
                "a worker-held task must never be typed into the live core")
            self.write_result("task-unrelated.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    # Two tests removed here: both ran next_pending_task()/
    # probe_optional_task_handler() directly, deleted by the single-decider redesign.


if __name__ == "__main__":
    unittest.main(verbosity=2)
