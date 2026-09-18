#!/usr/bin/env python3
"""Tests for the Claude Code task-file-injection notifier
(src/agent/claude/cli/task-notifier.sh) — the external tmux-injection
standby path, matching Codex/agy's shape.

Hermetic: a stub `tmux` on PATH stands in for the real binary. Pane content
and core-status.json are plain files the test controls directly, so
status-file/pane gating and staging verification are deterministic rather
than timing-races against a real TUI. `--event <filename>` drives one
dispatch directly (exercises has_result, idle-gating via both
core-status.json and the pane, staging-retry, submit-confirm-retry) without
needing the fswatch-driven main loop.

core_pane_is_idle_ready() delegates gate/idle-footer classification to the
REAL src/core-input-watch.py (not a stub) — that module already owns Claude's
pane-state patterns for the M0-M4 core supervisor, so this suite pins the
delegation rather than re-deriving the pattern list.

One additional test runs the real main loop against real fswatch to prove
the watch-tasks-stream.sh wiring itself (a task file appearing on disk
reaches the notifier and gets dispatched) — skipped if fswatch is absent.

Does NOT drive a real Claude Code session — that was verified by hand
against the real binary (see the PR body for the pane-text transcript this
suite's IDLE/BUSY markers are drawn from).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(os.environ.get(
    "SUTANDO_TEST_REPO", Path(__file__).resolve().parents[1]
)).resolve()

NOTIFIER = REPO / "src/agent/claude/cli/task-notifier.sh"

# Drawn from a live capture against real Claude Code v2.1.261 (see PR body).
IDLE_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
BUSY_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents"
TRUST_GATE_PANE = "\n".join([
    " Quick safety check: Is this a project you created or one you trust?",
    " ❯ No, exit",
    "   Yes, I trust this folder",
    " Enter to confirm · Esc to cancel",
])


class FakeTmuxHarness(unittest.TestCase):
    """Base: builds a stub `tmux` + isolated workspace for one test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tasks_dir = self.root / "workspace" / "tasks"
        self.results_dir = self.root / "workspace" / "results"
        self.state_dir = self.root / "workspace" / "state"
        self.logs_dir = self.root / "workspace" / "logs"
        for d in (self.tasks_dir, self.results_dir, self.state_dir, self.logs_dir):
            d.mkdir(parents=True)
        self.pane_file = self.root / "pane.txt"
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        self.status_file = self.state_dir / "core-status.json"
        self.write_status("idle")
        self.session_flag = self.root / "session.flag"
        self.session_flag.write_text("up")
        self.sendkeys_log = self.root / "send-keys.log"
        self.sendkeys_log.write_text("")
        self.swallow_flag = self.root / "swallow-next-paste.flag"
        self.busy_after_enter_flag = self.root / "busy-after-enter.flag"
        self._write_fake_tmux()

    def write_status(self, status, ts=None):
        self.status_file.write_text(json.dumps({
            "status": status,
            "ts": ts if ts is not None else time.time(),
        }))

    def _write_fake_tmux(self):
        # -l pastes append to pane.txt (simulating composer echo) unless
        # swallow-next-paste.flag is set (consumed once) — simulates a drop.
        script = self.bin / "tmux"
        script.write_text(f'''#!/bin/bash
[ "${{1:-}}" = -S ] && shift 2
cmd="$1"; shift
case "$cmd" in
  has-session)
    [ -f "{self.session_flag}" ] && exit 0
    exit 1
    ;;
  capture-pane)
    cat "{self.pane_file}" 2>/dev/null
    exit 0
    ;;
  send-keys)
    # args: -t SESSION[:0] [-l -- TEXT | C-m]
    shift 2  # -t SESSION
    if [ "${{1:-}}" = -l ]; then
      shift 2  # -l --
      text="$1"
      printf 'TYPE %s\\n' "$text" >> "{self.sendkeys_log}"
      if [ -f "{self.swallow_flag}" ]; then
        rm -f "{self.swallow_flag}"
      else
        printf '%s\\n' "$text" >> "{self.pane_file}"
      fi
    else
      printf 'ENTER\\n' >> "{self.sendkeys_log}"
      # Real Claude keeps the submitted prompt visible as scrollback (it
      # does not vanish from the pane) — only opt-in when a test wants to
      # prove the confirm check doesn't misread that as still-staged.
      if [ -f "{self.busy_after_enter_flag}" ]; then
        printf '%s\\n' "{BUSY_FOOTER}" >> "{self.pane_file}"
      fi
    fi
    exit 0
    ;;
  new-session|kill-session|setenv)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
''')
        script.chmod(0o755)

    def _env(self, extra=None):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            "SUTANDO_TMUX_SOCKET": str(self.root / "fake.sock"),
            "SUTANDO_TMUX_SESSION": "sutando-core-test",
            "SUTANDO_TASKS_DIR": str(self.tasks_dir),
            "SUTANDO_RESULTS_DIR": str(self.results_dir),
            "SUTANDO_CORE_STATUS_FILE": str(self.status_file),
            "SUTANDO_NOTIFIER_POLL_INTERVAL": "0.1",
            "SUTANDO_NOTIFIER_CORE_READY_TIMEOUT": "5",
            "SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT": "1",
            "SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "8",
        })
        if extra:
            env.update(extra)
        return env

    def write_task(self, name, body="task: say OK\n"):
        (self.tasks_dir / name).write_text(body)

    def write_result(self, name, body="OK\n"):
        (self.results_dir / name).write_text(body)

    def run_event(self, filename, env_extra=None, timeout=15):
        return subprocess.run(
            ["/bin/bash", str(NOTIFIER), "--event", filename],
            env=self._env(env_extra),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def sendkeys_log_text(self):
        return self.sendkeys_log.read_text()


class EventDispatchTests(FakeTmuxHarness):
    """--event <filename>: exercises submit_task/deliver_prompt directly."""

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

    def test_stale_running_status_falls_back_to_pane(self):
        # status.json says "running" but is stale (>90s): the notifier must
        # fall back to the pane's own idle-ready read rather than trust it.
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

    def test_fresh_running_status_blocks_dispatch(self):
        # A fresh "running" self-report is trusted outright, without
        # consulting the pane, and must not dispatch before it times out.
        self.write_task("task-fresh.txt")
        self.write_status("running", ts=time.time())
        result = self.run_event("task-fresh.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a fresh 'running' self-report must block dispatch")

    def test_idle_status_but_busy_pane_blocks_dispatch_until_idle(self):
        # status.json says "idle" but the pane shows an in-flight turn: the
        # pane must veto the stale-looking idle self-report.
        self.write_task("task-c.txt")
        self.write_status("idle")
        self.pane_file.write_text(BUSY_FOOTER + "\n")

        import threading
        violation = []

        def _unblock():
            time.sleep(0.6)  # several poll intervals while still busy
            if self.sendkeys_log_text() != "":
                violation.append("notifier dispatched while pane was still busy")
            self.pane_file.write_text(IDLE_FOOTER + "\n")
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_unblock)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(violation, [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-c.txt", self.sendkeys_log_text())

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

    def test_unconfirmed_submit_is_re_pressed(self):
        # This stub's ENTER never clears the staged marker, so deliver_prompt
        # must re-press C-m at least once after the confirm timeout elapses.
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

    def test_submit_confirms_on_busy_pane_not_on_text_vanishing(self):
        # Live-caught regression (see PR body): the submitted text stays in
        # scrollback, so confirm must key on the pane going BUSY, not on it.
        self.write_task("task-h.txt")
        self.busy_after_enter_flag.write_text("1")
        result = self.run_event("task-h.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("ENTER"), 1,
                          "a pane that goes busy right after C-m must be recognized as "
                          "confirmed on the first attempt, not re-pressed")
        self.assertIn("Sutando task ready: task-h.txt", self.pane_file.read_text(),
                       "the submitted text staying in scrollback is the exact case this pins")

    def test_no_status_file_blocks_dispatch(self):
        # No self-report at all yet (e.g. before the core's first status
        # write): must not guess idle from the pane alone.
        self.status_file.unlink()
        self.write_task("task-g.txt")
        result = self.run_event("task-g.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "")


class MainLoopWiringTest(FakeTmuxHarness):
    """Proves the actual claim: a task file dropped on disk reaches the
    notifier via watch-tasks-stream.sh's real fswatch pipeline, unaided by
    --event. Everything else about dispatch is already covered above."""

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
            time.sleep(1.5)  # let fswatch attach before the file appears
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


if __name__ == "__main__":
    unittest.main()
