#!/usr/bin/env python3
"""The task notifiers can no longer spin or outlive a TERM, and their supervisor can no longer pile up.

2026-11-01: a claude task notifier ran at 60-90% CPU for about 9 h and needed SIGKILL. Its
standby watcher had closed the events FIFO without finishing its exit. Every timed read
then returned EOF at once (bash 3.2 reports EOF as 1, the same as a timeout), `kill -0`
still answered, and the loop never slept. TERM's cleanup then `wait`ed on that watcher
forever, and the supervisor waited on the notifier forever, outliving its tmux session.

Each case runs the REAL script against a fake repo whose src/watch-tasks-stream.sh is a
stub. Everything else in the fake repo is a link to this repo.
- Each notifier, given a watcher that closes its pipe and lingers ignoring TERM: it
  notices, stops that watcher (KILL after the grace) and exits by itself, using little
  CPU on the way.
- Each notifier, given a healthy watcher that ignores TERM: a TERM to the notifier ends
  it within the grace.
- The guard helpers on their own.
- The supervisor: a bounded child stop, the restart backoff, and one supervisor per
  inbox (an older one for the same inbox is replaced; a pid that is no supervisor is left alone).
- helper_cpu_guard, the health-check probe that names a helper spinning for 10+ min.

Run: python3 tests/notifier-spin-guard.test.py   (macOS or Linux; bash on PATH)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import helper_cpu_guard as hcg  # noqa: E402

GUARD = REPO / "src" / "notifier-guard.sh"
SUPERVISOR = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"

STUCK_WATCHER = """#!/bin/bash
# Stuck mid-exit: the events FIFO is closed, the process lingers and ignores TERM.
trap '' TERM
echo $$ > "$WATCHER_PID_FILE"
exec 1>&- 9>&-
while :; do sleep 1; done
"""
DEAF_WATCHER = """#!/bin/bash
# Healthy (its stdout stays open), but deaf to TERM.
trap '' TERM
echo $$ > "$WATCHER_PID_FILE"
while :; do sleep 1; done
"""


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout
    return hcg.parse_clock(out) or 0.0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


class FakeRepo:
    def __init__(self, notifier: str, watcher: str):
        self.tmp = Path(tempfile.mkdtemp(prefix="notif-guard."))
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        for e in (REPO / "src").iterdir():
            if e.name not in ("watch-tasks-stream.sh", "agent"):
                (self.repo / "src" / e.name).symlink_to(e)
        for e in (REPO / "scripts").iterdir():
            (self.repo / "scripts" / e.name).symlink_to(e)
        rel = Path("src/agent") / notifier
        (self.repo / rel).parent.mkdir(parents=True)
        (self.repo / rel).write_text((REPO / rel).read_text())
        (self.repo / "src" / "watch-tasks-stream.sh").write_text(watcher)
        self.notifier = self.repo / rel
        self.ws = self.tmp / "ws"
        for d in ("tasks", "results", "logs", "state"):
            (self.ws / d).mkdir(parents=True)
        self.watcher_pid_file = self.tmp / "watcher.pid"

    def start(self) -> subprocess.Popen:
        env = dict(os.environ, SUTANDO_TASKS_DIR=str(self.ws / "tasks"), SUTANDO_WORKSPACE_DIR=str(self.ws),
                   SUTANDO_TMUX_SOCKET=str(self.tmp / "no-tmux.sock"), WATCHER_PID_FILE=str(self.watcher_pid_file),
                   SUTANDO_NOTIFIER_RETRY_POLL_SEC="5", SUTANDO_NOTIFIER_RETRY_INTERVAL="5")
        env.pop("SUTANDO_INSTANCE_ID", None)
        # stderr to a file: an unread pipe fills and blocks the notifier mid-write, and the
        # watcher inherits it, so a read of it would wait on the watcher too.
        self.err = self.tmp / "notifier.err"
        with open(self.err, "w") as err:
            return subprocess.Popen(["bash", str(self.notifier)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=err, start_new_session=True)

    def watcher_pid(self, timeout=10) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return int(self.watcher_pid_file.read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("the stub watcher never started")


def reap(proc: subprocess.Popen, *pids: int) -> None:
    for p in (proc.pid, *pids):
        try:
            os.killpg(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


class NotifierEndsAClosedStream(unittest.TestCase):
    def run_case(self, notifier: str, log_name: str):
        f = FakeRepo(notifier, STUCK_WATCHER)
        proc = f.start()
        wpid = f.watcher_pid()
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            cpu = cpu_seconds(proc.pid)
            reap(proc, wpid)
            self.fail(f"{notifier} kept running on a closed events pipe (cpu {cpu:.1f}s)")
        self.assertIsNotNone(rc)
        deadline = time.time() + 5
        while alive(wpid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(wpid), "the stuck watcher was left running")
        self.assertIn("closed its events pipe but is still running", f.err.read_text())
        logged = (f.ws / "logs" / log_name).read_text()
        self.assertIn("closed its events pipe", logged)

    def test_the_claude_notifier(self):
        self.run_case("claude/cli/task-notifier.sh", "claude-task-notifier.log")

    def test_the_codex_notifier(self):
        self.run_case("codex/cli/task-notifier.sh", "task-notifier.log")


class NotifierHonoursTerm(unittest.TestCase):
    def run_case(self, notifier: str):
        f = FakeRepo(notifier, DEAF_WATCHER)
        proc = f.start()
        wpid = f.watcher_pid()
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "a live watcher's notifier keeps running")
        self.assertLess(cpu_seconds(proc.pid), 1.0, "an idle notifier must not burn CPU")
        os.kill(proc.pid, signal.SIGTERM)
        # Bounded, not instant: the watcher gets a 3 s grace, then the notifier's own
        # cleanup runs (one process-table scan), which a loaded host slows to seconds.
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            reap(proc, wpid)
            self.fail(f"{notifier} ignored TERM")
        deadline = time.time() + 5
        while alive(wpid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(wpid), "TERM left the watcher running")

    def test_the_claude_notifier(self):
        self.run_case("claude/cli/task-notifier.sh")

    def test_the_codex_notifier(self):
        self.run_case("codex/cli/task-notifier.sh")


def bash(script: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, **env))


class Guard(unittest.TestCase):
    def test_a_stream_ended_by_eof_and_by_a_dead_or_zombie_writer(self):
        r = bash(f""". "{GUARD}"
sleep 30 & live=$!
( exit 0 ) & dead=$!; sleep 0.3
events_stream_ended "$live" 0 30 && echo early-eof
events_stream_ended "$live" 30 30 || echo timeout
events_stream_ended "$live" 0 1 || echo short-timeout-undecidable
events_stream_ended "$dead" 30 30 && echo dead-writer
events_stream_ended "" 30 30 && echo no-writer
kill $live""")
        self.assertEqual(r.stdout.split(), ["early-eof", "timeout", "short-timeout-undecidable", "dead-writer", "no-writer"])

    def test_stop_group_bounded_kills_a_child_that_ignores_term(self):
        r = bash(f""". "{GUARD}"
python3 -c 'import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)' & c=$!
sleep 0.3
s=$SECONDS; stop_group_bounded "$c" 1; echo "took $((SECONDS - s))"
kill -0 "$c" 2>/dev/null && echo still-alive || echo gone""")
        took = int(r.stdout.split()[1])
        self.assertLessEqual(took, 3)
        self.assertIn("gone", r.stdout)

    def test_stop_group_bounded_with_no_pid_and_a_gone_pid(self):
        r = bash(f""". "{GUARD}"; stop_group_bounded ""; stop_group_bounded 999999 1; echo fine""")
        self.assertEqual(r.stdout.strip(), "fine")


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-guard."))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "state").mkdir()

    def sourced(self, body: str) -> subprocess.CompletedProcess:
        return bash(f"""export SUTANDO_SUPERVISOR_SOURCE_ONLY=1
. "{SUPERVISOR}"
{body}""", SUTANDO_TASKS_DIR=str(self.ws / "tasks"), SUTANDO_WORKSPACE_DIR=str(self.ws),
                    SUTANDO_TMUX_SOCKET=str(self.tmp / "no.sock"))

    def test_stop_child_is_bounded(self):
        r = self.sourced("""python3 -c 'import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)' & child_pid=$!
c=$child_pid; sleep 0.3
s=$SECONDS; stop_child; echo "took $((SECONDS - s))"
kill -0 "$c" 2>/dev/null && echo still-alive || echo gone""")
        self.assertLessEqual(int(r.stdout.split()[1]), 5, r.stderr)
        self.assertIn("gone", r.stdout)

    def test_restart_backoff_doubles_on_short_lives_and_resets(self):
        r = self.sourced("""RESTART_DELAY=1 RESTART_DELAY_MAX=8 RESTART_HEALTHY_S=30
for life in 0 0 0 0 0 0 45 0; do next_restart_delay $life; printf '%s ' "$NEXT_DELAY"; done""")
        self.assertEqual(r.stdout.split(), ["2", "4", "8", "8", "8", "8", "1", "2"])
        r = self.sourced("""RESTART_DELAY=0.01; next_restart_delay 0; echo "$NEXT_DELAY" """)
        self.assertEqual(r.stdout.strip(), "0.01", "a fractional delay is used as given")

    def test_an_older_supervisor_for_the_same_inbox_is_replaced(self):
        fake = self.tmp / "task-notifier-supervisor.sh"
        fake.write_text("trap '' TERM\nwhile :; do sleep 1; done\n")
        old = subprocess.Popen(["bash", str(fake)], start_new_session=True)
        try:
            r = self.sourced(f"""f="$(supervisor_slot_file)"; mkdir -p "$(dirname "$f")"
printf '%s\\n%s\\n' {old.pid} "$TASKS_DIR" > "$f"
claim_supervisor_slot; sed -n 1p "$f"; echo "me $$" """)
            lines = r.stdout.split("\n")
            self.assertEqual(lines[0], r.stdout.split("me ")[1].strip(), r.stderr)
            self.assertIn(f"replacing the older supervisor (pid {old.pid})", r.stderr)
            old.wait(timeout=10)
        finally:
            if old.poll() is None:
                old.kill()

    def test_a_recorded_pid_that_is_no_supervisor_is_left_alone(self):
        other = subprocess.Popen(["sleep", "30"])
        try:
            r = self.sourced(f"""f="$(supervisor_slot_file)"; mkdir -p "$(dirname "$f")"
printf '%s\\n%s\\n' {other.pid} "$TASKS_DIR" > "$f"
claim_supervisor_slot; release_supervisor_slot; [ -e "$f" ] && echo kept || echo released""")
            self.assertIsNone(other.poll(), "an unrelated process was killed")
            self.assertNotIn("replacing", r.stderr)
            self.assertEqual(r.stdout.strip(), "released")
        finally:
            other.kill()


class HelperCpu(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "samples.json"
        self.repo = Path("/r/sutando")

    def rows(self, cpu, etime=5000, pid=42, cmd="bash /r/sutando/src/agent/claude/cli/task-notifier.sh"):
        return [{"pid": pid, "etime": etime, "cpu": cpu, "command": cmd}]

    def test_clock_parsing(self):
        self.assertEqual(hcg.parse_clock("01:02"), 62)
        self.assertEqual(hcg.parse_clock("1:00:00"), 3600)
        self.assertEqual(hcg.parse_clock("2-00:00:01"), 172801)
        self.assertAlmostEqual(hcg.parse_clock("0:12.50"), 12.5)
        for bad in ("", "x:y", "a-01:00", "1:2:3:4"):
            self.assertIsNone(hcg.parse_clock(bad))

    def test_only_interpreters_running_repo_scripts_are_helpers(self):
        self.assertEqual(hcg.helper_script("bash /r/sutando/src/x.sh --a", self.repo), "src/x.sh")
        self.assertEqual(hcg.helper_script("/usr/bin/python3.12 /r/sutando/src/y.py", self.repo), "src/y.py")
        self.assertEqual(hcg.helper_script("/x/Python.app/Contents/MacOS/Python /r/sutando/z.py", self.repo), "z.py")
        self.assertIsNone(hcg.helper_script("claude --plugin-dir /r/sutando/skills/a", self.repo))
        self.assertIsNone(hcg.helper_script("bash /elsewhere/x.sh", self.repo))
        self.assertIsNone(hcg.helper_script("", self.repo))

    def test_a_helper_hot_for_the_whole_window_is_named(self):
        self.assertEqual(hcg.evaluate(self.rows(100), self.repo, self.state, now=10_000), [])
        self.assertEqual(hcg.evaluate(self.rows(300, etime=5300), self.repo, self.state, now=10_300), [],
                         "inside the window: no verdict yet, the old sample is kept")
        hot = hcg.evaluate(self.rows(100 + 0.8 * 700, etime=5700), self.repo, self.state, now=10_700)
        self.assertEqual(hot, [{"pid": 42, "script": "src/agent/claude/cli/task-notifier.sh",
                                "cpu_pct": 80, "minutes": 12}])

    def test_a_quiet_helper_a_reused_pid_and_a_gone_process(self):
        hcg.evaluate(self.rows(100), self.repo, self.state, now=10_000)
        self.assertEqual(hcg.evaluate(self.rows(110, etime=5700), self.repo, self.state, now=10_700), [])
        hcg.evaluate(self.rows(100), self.repo, self.state, now=20_000)
        self.assertEqual(hcg.evaluate(self.rows(900, etime=30), self.repo, self.state, now=20_700), [],
                         "the same pid with a new start time starts over")
        hcg.evaluate([], self.repo, self.state, now=21_000)
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_the_probe_warns_by_name_and_survives_bad_input(self):
        # 700 s before the probe's reading (etime 5000), the same process at etime 4300.
        hcg.evaluate(self.rows(0, etime=4300), self.repo, self.state, now=time.time() - 700)

        class Out:
            stdout = "  42  1:23:20  11:40.00 bash /r/sutando/src/agent/claude/cli/task-notifier.sh\nnoise\n"
        r = hcg.check(self.repo, self.state, runner=lambda *a, **k: Out())
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("pid 42 src/agent/claude/cli/task-notifier.sh", r["detail"])
        self.state.write_text("[]")
        self.assertEqual(hcg.check(self.repo, self.state, runner=lambda *a, **k: Out())["status"], "ok")

        def boom(*a, **k):
            raise OSError("no ps")
        self.assertEqual(hcg.check(self.repo, self.state, runner=boom)["status"], "ok")

    def test_health_check_runs_the_probe(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_helper_cpu())", src)
        self.assertIn('helper_cpu_guard.check(REPO_DIR, WORKSPACE_DIR / "state" / "helper-cpu-samples.json")', src)


if __name__ == "__main__":
    unittest.main()
