#!/usr/bin/env python3
"""Tests for the agy (Antigravity CLI) task-file-injection notifier — Slice 2
of sonichi#4272 (src/agent/agy/cli/task-notifier.sh).

Hermetic: a stub `tmux` on PATH stands in for the real binary. Pane content
is a plain file the test controls directly, so busy/idle gating and staging
verification are deterministic rather than timing-races against a real TUI.
`--event <filename>` drives one dispatch directly (exercises has_result,
idle-gating, staging-retry, submit-confirm-retry — everything submit_task/
deliver_prompt do) without needing the fswatch-driven main loop. One
additional test runs the real main loop against real fswatch to prove the
watch-tasks-stream.sh wiring itself (a task file appearing on disk reaches
the notifier and gets dispatched). A final class exercises start-cli.sh's
ensure_task_notifier: does a launch actually start the watcher session, and
does a second invocation avoid starting a duplicate one.

Does NOT drive a real agy CLI + real Google auth — see
tests/agy-start-cli.test.py's docstring for why (same constraint, same
reason). That was instead verified by hand against the real binary:
sonichi#4272 slice 2 PR body has the transcript.
"""
from __future__ import annotations

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

NOTIFIER = REPO / "src/agent/agy/cli/task-notifier.sh"

BUSY_MARKER = "esc to cancel"
IDLE_MARKER = "? for shortcuts"


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
        self.logs_dir = self.root / "workspace" / "logs"
        for d in (self.tasks_dir, self.results_dir, self.logs_dir):
            d.mkdir(parents=True)
        self.pane_file = self.root / "pane.txt"
        self.pane_file.write_text(IDLE_MARKER + "\n")
        self.composer_file = self.root / "composer.txt"
        self.composer_file.write_text("")
        self.session_flag = self.root / "session.flag"
        self.session_flag.write_text("up")
        self.sendkeys_log = self.root / "send-keys.log"
        self.sendkeys_log.write_text("")
        self.swallow_flag = self.root / "swallow-next-paste.flag"
        self.pane_width = None  # None = no wrap; a subclass/test may set a column count
        self._write_fake_tmux()

    def _write_fake_tmux(self):
        # Renders pane.txt+composer.txt padded to a FIXED row count; pane_width,
        # when set, hard-wraps the composer like a real narrow terminal (`fold`).
        script = self.bin / "tmux"
        pane_width = self.pane_width if self.pane_width else 0
        script.write_text(f'''#!/bin/bash
[ "${{1:-}}" = -S ] && shift 2
cmd="$1"; shift
FIXED_ROWS=12
PANE_WIDTH={pane_width}
case "$cmd" in
  has-session)
    [ -f "{self.session_flag}" ] && exit 0
    exit 1
    ;;
  capture-pane)
    hist=""
    [ -f "{self.pane_file}" ] && hist="$(cat "{self.pane_file}")"
    composer="$(cat "{self.composer_file}" 2>/dev/null)"
    if [ "$PANE_WIDTH" -gt 0 ] && [ -n "$composer" ]; then
      composer="$(printf '%s' "$composer" | fold -w "$PANE_WIDTH")"
    fi
    if [ -n "$hist" ]; then
      combined="$hist"$'\\n'"$composer"
    else
      combined="$composer"
    fi
    n=$(printf '%s\\n' "$combined" | wc -l | tr -d ' ')
    if [ "$n" -gt "$FIXED_ROWS" ]; then
      printf '%s\\n' "$combined" | tail -n "$FIXED_ROWS"
    else
      pad=$((FIXED_ROWS - n))
      i=0
      while [ "$i" -lt "$pad" ]; do printf '\\n'; i=$((i + 1)); done
      printf '%s\\n' "$combined"
    fi
    exit 0
    ;;
  send-keys)
    # args: -t SESSION [-l -- TEXT | Enter]
    shift 2  # -t SESSION
    if [ "${{1:-}}" = -l ]; then
      shift 2  # -l --
      text="$1"
      printf 'TYPE %s\\n' "$text" >> "{self.sendkeys_log}"
      if [ -f "{self.swallow_flag}" ]; then
        rm -f "{self.swallow_flag}"
      else
        printf '%s' "$(cat "{self.composer_file}" 2>/dev/null)$text" > "{self.composer_file}"
      fi
    else
      if [ -s "{self.composer_file}" ]; then
        cat "{self.composer_file}" >> "{self.pane_file}"
        printf '\\n' >> "{self.pane_file}"
        : > "{self.composer_file}"
      fi
      printf 'ENTER\\n' >> "{self.sendkeys_log}"
    fi
    exit 0
    ;;
  new-session|kill-session|has-session|setenv)
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
            "SUTANDO_AGY_TMUX_SOCKET": str(self.root / "fake.sock"),
            "SUTANDO_AGY_TMUX_SESSION": "sutando-agy-test",
            "SUTANDO_TASKS_DIR": str(self.tasks_dir),
            "SUTANDO_RESULTS_DIR": str(self.results_dir),
            "SUTANDO_AGY_NOTIFIER_POLL_INTERVAL": "0.1",
            "SUTANDO_AGY_NOTIFIER_CORE_READY_TIMEOUT": "5",
            "SUTANDO_AGY_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT": "1",
            "SUTANDO_AGY_NOTIFIER_COMPLETION_TIMEOUT": "8",
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

    def test_pending_task_is_typed_and_submitted(self):
        self.write_task("task-b.txt")
        # Completion must be observed for --event to return; write it from a
        # background thread shortly after dispatch so the wait loop exits.
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

    def test_long_filename_wrapped_across_pane_rows_still_stages_once(self):
        # A long filename can hard-wrap the marker line across two physical
        # pane rows in a narrow terminal; staging must still detect it.
        self.pane_width = 80
        self._write_fake_tmux()
        filename = "task-cron-sutando-life-kewei-overview-hourly-screenshot-1787587200.txt"
        self.write_task(filename)
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result(filename)
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event(filename)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        type_calls = log.count(f"TYPE Sutando task ready: {filename}")
        self.assertEqual(type_calls, 1,
                          f"expected exactly one type attempt, got {type_calls}:\n{log}")

    def test_busy_pane_blocks_dispatch_until_idle(self):
        self.write_task("task-c.txt")
        self.pane_file.write_text(BUSY_MARKER + "\n")

        # An assert inside a background thread never fails the test; record
        # the violation and check it from the main thread after join.
        import threading
        violation = []

        def _unblock():
            time.sleep(0.6)  # several poll intervals while still busy
            if self.sendkeys_log_text() != "":
                violation.append("notifier dispatched while pane was still busy")
            self.pane_file.write_text(IDLE_MARKER + "\n")
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

    def test_dropped_paste_is_retyped(self):
        self.write_task("task-d.txt")
        self.swallow_flag.write_text("1")  # first -l paste vanishes, unstaged

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

    def test_stale_history_marker_does_not_count_as_staged(self):
        # A prior dispatch of the SAME filename left its marker in pane
        # history — the marker text is byte-identical across dispatches.
        self.write_task("task-stale.txt")
        self.pane_file.write_text(
            IDLE_MARKER + "\nSutando task ready: task-stale.txt\n" + IDLE_MARKER + "\n"
        )
        self.swallow_flag.write_text("1")  # this attempt's paste vanishes, unstaged

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
        type_calls = self.sendkeys_log_text().count("TYPE Sutando task ready: task-stale.txt")
        self.assertEqual(
            type_calls, 2,
            "a swallowed paste must still be retyped even though an earlier "
            "dispatch's marker for the same task is already in pane history",
        )

    def test_unconfirmed_submit_is_re_pressed(self):
        # This stub's ENTER never clears the staged marker, so deliver_prompt
        # must re-press Enter once after the confirm timeout elapses.
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

    def test_missing_results_dir_is_created_before_any_dispatch(self):
        # agy publishes results ITSELF (the watcher only mkdirs TASKS_DIR), so
        # a first-run RESULTS_DIR must exist before agy ever tries to write there.
        fresh_results = self.root / "workspace" / "results-fresh"
        self.assertFalse(fresh_results.exists())
        self.session_flag.unlink()  # no session -> submit_task drops immediately
        self.write_task("task-g.txt")
        result = self.run_event("task-g.txt", env_extra={"SUTANDO_RESULTS_DIR": str(fresh_results)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fresh_results.is_dir(),
                         "RESULTS_DIR must be created at startup, not left for agy to hit ENOENT on")


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

    def test_spawned_watcher_gets_a_distinct_sentinel_not_the_canonical_one(self):
        # Without a bound instance id the agy watcher's sentinel collides with
        # a canonical watcher's on the same host (both resolve to the bare name).
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        env = self._env()
        # This session's own ambient identity env vars would otherwise make the
        # sentinel non-canonical on their own, confounding the check below.
        for var in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID"):
            env.pop(var, None)
        state_dir = self.tasks_dir.parent / "state"
        canonical_sentinel = state_dir / "watch-tasks-stream.pid"
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=env,
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not state_dir.exists():
                time.sleep(0.2)
            sentinels = []
            deadline = time.time() + 10
            while time.time() < deadline:
                sentinels = list(state_dir.glob("watch-tasks-stream*.pid"))
                if sentinels:
                    break
                time.sleep(0.2)
            self.assertTrue(sentinels, "watcher never wrote a sentinel")
            self.assertNotEqual(
                sentinels, [canonical_sentinel],
                "agy watcher's only sentinel is the bare canonical name — "
                "it would collide with a real canonical watcher on this host")
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

    def test_a_foreign_nonempty_inherited_instance_id_is_overridden_not_kept(self):
        # A bare fallback (`${SUTANDO_INSTANCE_ID:-agy-task-notifier}`) misses a
        # NON-empty inherited value; it must be replaced, never kept.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        env = self._env()
        for var in ("SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID"):
            env.pop(var, None)
        env["SUTANDO_INSTANCE_ID"] = "shared-worker"
        state_dir = self.tasks_dir.parent / "state"
        foreign_sentinel = state_dir / "watch-tasks-stream-local-agent+shared-worker.pid"
        expected_sentinel = state_dir / "watch-tasks-stream-local-agent+agy-task-notifier.pid"
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=env,
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not expected_sentinel.exists() and not foreign_sentinel.exists():
                time.sleep(0.2)
            self.assertTrue(
                expected_sentinel.exists(),
                f"expected {expected_sentinel.name}, got: "
                f"{[p.name for p in state_dir.glob('watch-tasks-stream*.pid')]}")
            self.assertFalse(foreign_sentinel.exists(),
                              "the inherited foreign instance id leaked into the sentinel name")
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


class StartCliNotifierWiringTest(unittest.TestCase):
    """start-cli.sh's ensure_task_notifier: does a launch actually start the
    watcher session, and does a second invocation avoid a duplicate. Needs a
    session-name-aware tmux stub (agy-start-cli.test.py's stub tracks a
    single shared marker regardless of session name, which is why that
    existing suite can't see this behavior)."""

    LAUNCHER = REPO / "src/agent/agy/cli/start-cli.sh"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.tmux_log = self.root / "tmux.log"
        self.tmux_log.write_text("")
        self._write_fake_agy()
        self._write_fake_fswatch()
        self._write_fake_tmux()

    def _write_fake_agy(self):
        path = self.bin / "agy"
        path.write_text('''#!/bin/bash
if [ "${1:-}" = --version ]; then echo "9.9.9-fake"; exit 0; fi
if [ "${1:-}" = models ]; then echo "fake-model"; exit 0; fi
exec sleep 300
''')
        path.chmod(0o755)

    def _write_fake_fswatch(self):
        # This stub tmux never execs the notifier, so only PRESENCE matters
        # here — see agy-notifier-dependency-gate.test.py for real liveness.
        path = self.bin / "fswatch"
        path.write_text("#!/bin/bash\nexit 0\n")
        path.chmod(0o755)

    def _write_fake_tmux(self):
        # Per-session marker files, unlike agy-start-cli.test.py's single
        # shared flag — needed to see the watcher session independently.
        script = self.bin / "tmux"
        script.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> "{self.tmux_log}"
[ "${{1:-}}" = -S ] && shift 2
cmd="$1"; shift
case "$cmd" in
  has-session)
    name="${{2#=}}"
    [ -f "{self.sessions_dir}/$name" ] && exit 0
    exit 1
    ;;
  new-session)
    # Find "-s NAME" among the remaining args.
    name=""
    prev=""
    for a in "$@"; do
      [ "$prev" = -s ] && name="$a"
      prev="$a"
    done
    [ -n "$name" ] && touch "{self.sessions_dir}/$name"
    exit 0
    ;;
  attach)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
''')
        script.chmod(0o755)

    def _env(self):
        env = dict(os.environ)
        env.pop("SUTANDO_CORE_SESSION", None)
        env.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "SUTANDO_AGY_TMUX_SOCKET": str(self.root / "fake.sock"),
            "SUTANDO_AGY_TMUX_SESSION": "sutando-agy-wiretest",
            "SUTANDO_AGY_ONBOARDING_PATH": str(self.root / "onboarding.json"),
            "SUTANDO_TASKS_DIR": str(self.root / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.root / "results"),
            "HOME": str(self.root),
        })
        return env

    def run_launcher(self):
        return subprocess.run(
            ["/bin/bash", str(self.LAUNCHER)],
            env=self._env(),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_launch_also_starts_the_watcher_session(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.sessions_dir / "sutando-agy-wiretest").exists(),
                         "core session was not started")
        self.assertTrue((self.sessions_dir / "sutando-agy-wiretest-watcher").exists(),
                         "ensure_task_notifier never started the watcher session")
        log = self.tmux_log.read_text()
        self.assertIn("task-notifier.sh", log)

    def test_watcher_launch_binds_instance_id_and_clears_unset_dirs(self):
        # Omitting -e leaves whatever the tmux server's global env carries;
        # the launcher must always pass -e, never gate it on local emptiness.
        env = self._env()
        env.pop("SUTANDO_TASKS_DIR", None)
        env.pop("SUTANDO_RESULTS_DIR", None)
        result = subprocess.run(
            ["/bin/bash", str(self.LAUNCHER)], env=env, cwd=str(self.root),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        watcher_lines = [ln for ln in self.tmux_log.read_text().splitlines()
                         if "new-session" in ln and "task-notifier.sh" in ln]
        self.assertEqual(len(watcher_lines), 1, self.tmux_log.read_text())
        line = watcher_lines[0]
        self.assertIn("SUTANDO_INSTANCE_ID=agy-task-notifier", line)
        self.assertIn("-e SUTANDO_TASKS_DIR=", line,
                       "TASKS_DIR must be explicitly bound (even empty), never omitted")
        self.assertIn("-e SUTANDO_RESULTS_DIR=", line,
                       "RESULTS_DIR must be explicitly bound (even empty), never omitted")

    def test_second_invocation_does_not_duplicate_the_watcher(self):
        first = self.run_launcher()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        new_session_calls_after_first = self.tmux_log.read_text().count("new-session")
        second = self.run_launcher()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        new_session_calls_after_second = self.tmux_log.read_text().count("new-session")
        self.assertEqual(
            new_session_calls_after_first, new_session_calls_after_second,
            "a second invocation must not start a duplicate watcher session",
        )


if __name__ == "__main__":
    unittest.main()
