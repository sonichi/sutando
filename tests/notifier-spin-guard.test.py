#!/usr/bin/env python3
"""The task notifiers end on a closed events pipe and honour TERM; their supervisor holds one slot per inbox.

- Each notifier, against a stub watcher that closes its pipe and lingers ignoring TERM, exits by
  itself and leaves no watcher. Against a healthy stub deaf to TERM, a TERM ends both, bounded.
- The guard helpers: stream end, zombie detection, and a group stop that kills members which
  outlive their leader.
- The supervisor: a bounded child stop; the restart backoff; and the slot, run for real. Two
  supervisors on one inbox (two spellings of it) leave one, and nothing is signalled. A waiter
  takes over once the holder exits.
- helper_cpu_guard: the script operand, never an option value; paths with spaces; relative
  launches; inline code; exact start-time keys; a locked sample file; unknown states.

Every real script runs against a fake repo whose src/watch-tasks-stream.sh is a stub (the rest is
linked to this repo), in an isolated environment, and every fixture is bound to this test.

Run: python3 tests/notifier-spin-guard.test.py   (macOS or Linux; bash on PATH)
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402
from _helpers.process_group import popen_bound  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import helper_cpu_guard as hcg  # noqa: E402
import holder_lock  # noqa: E402

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


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    return not alive(pid)


def kill_quietly(*pids: int) -> None:
    for p in pids:
        for target in (-p, p):
            try:
                os.kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


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
        self.err = self.tmp / "notifier.err"

    def start(self) -> subprocess.Popen:
        env = isolated_child_env(SUTANDO_TASKS_DIR=str(self.ws / "tasks"), SUTANDO_WORKSPACE_DIR=str(self.ws),
                                 SUTANDO_TMUX_SOCKET=str(self.tmp / "no-tmux.sock"),
                                 WATCHER_PID_FILE=str(self.watcher_pid_file),
                                 SUTANDO_NOTIFIER_RETRY_POLL_SEC="3", SUTANDO_NOTIFIER_RETRY_INTERVAL="3")
        env.pop("SUTANDO_INSTANCE_ID", None)
        # stderr to a file: an unread pipe fills and blocks the notifier, and the watcher inherits it.
        with open(self.err, "w") as err:
            return popen_bound(["bash", str(self.notifier)], env=env, stdout=subprocess.DEVNULL, stderr=err)

    def watcher_pid(self, timeout=10) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return int(self.watcher_pid_file.read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("the stub watcher never started")


class NotifierEndsAClosedStream(unittest.TestCase):
    def run_case(self, notifier: str, log_name: str):
        f = FakeRepo(notifier, STUCK_WATCHER)
        proc = f.start()
        wpid = f.watcher_pid()
        self.addCleanup(kill_quietly, proc.pid, wpid)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.fail(f"{notifier} kept running on a closed events pipe (cpu {cpu_seconds(proc.pid):.1f}s)")
        self.assertTrue(wait_gone(wpid), "the stuck watcher was left running")
        self.assertIn("closed its events pipe but is still running", f.err.read_text())
        self.assertIn("closed its events pipe", (f.ws / "logs" / log_name).read_text())

    def test_the_claude_notifier(self):
        self.run_case("claude/cli/task-notifier.sh", "claude-task-notifier.log")

    def test_the_codex_notifier(self):
        self.run_case("codex/cli/task-notifier.sh", "task-notifier.log")


class NotifierHonoursTerm(unittest.TestCase):
    def run_case(self, notifier: str):
        f = FakeRepo(notifier, DEAF_WATCHER)
        proc = f.start()
        wpid = f.watcher_pid()
        self.addCleanup(kill_quietly, proc.pid, wpid)
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "a live watcher's notifier keeps running")
        self.assertLess(cpu_seconds(proc.pid), 1.0, "an idle notifier must not burn CPU")
        os.kill(proc.pid, signal.SIGTERM)
        # Bounded, not instant: the watcher's 3 s grace, then the notifier's own cleanup (a loaded host is slow).
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail(f"{notifier} ignored TERM")
        self.assertTrue(wait_gone(wpid), "TERM left the watcher running")

    def test_the_claude_notifier(self):
        self.run_case("claude/cli/task-notifier.sh")

    def test_the_codex_notifier(self):
        self.run_case("codex/cli/task-notifier.sh")


def bash(script: str, cwd=None, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=cwd,
                          env=isolated_child_env(**env))


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

    def test_a_zombie_is_not_live_though_it_answers_kill_0(self):
        # This test process forks a child and does not reap it, so the child is a zombie meanwhile.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            time.sleep(0.5)
            r = bash(f'. "{GUARD}"; notifier_proc_live {pid} && echo live || echo not-live; '
                     f'kill -0 {pid} 2>/dev/null && echo signalable')
            self.assertEqual(r.stdout.split(), ["not-live", "signalable"], r.stderr)
        finally:
            os.waitpid(pid, 0)

    def test_stop_group_bounded_kills_a_leader_that_ignores_term(self):
        r = bash(f""". "{GUARD}"
python3 -c 'import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)' & c=$!
sleep 0.3
s=$SECONDS; stop_group_bounded "$c" 1; echo "took $((SECONDS - s))"
kill -0 "$c" 2>/dev/null && echo still-alive || echo gone""")
        self.assertLessEqual(int(r.stdout.split()[1]), 3)
        self.assertIn("gone", r.stdout)

    def test_stop_group_bounded_kills_members_that_outlive_their_leader(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = bash(f""". "{GUARD}"
python3 -c '
import os, signal, time
os.setsid()
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open("member.pid", "w").write(str(os.getpid()))
    time.sleep(60)
else:
    time.sleep(60)' & leader=$!
sleep 0.8; member=$(cat member.pid)
stop_group_bounded "$leader" 1
sleep 0.2; kill -0 "$member" 2>/dev/null && echo member-alive || echo member-gone""", cwd=tmp)
        self.assertIn("member-gone", r.stdout, r.stderr)

    def test_stop_group_bounded_with_no_pid_and_a_gone_pid(self):
        r = bash(f""". "{GUARD}"; stop_group_bounded ""; stop_group_bounded 999999 1; echo fine""")
        self.assertEqual(r.stdout.strip(), "fine")


class SupervisorHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-guard."))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "state").mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

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


TMUX_STUB = """#!/bin/bash
[ "${1:-}" = -S ] && shift 2
case "${1:-}" in has-session|list-panes) exit 0 ;; capture-pane) exit 1 ;; esac
exit 0
"""
IDLE_NOTIFIER = "#!/bin/bash\nwhile :; do sleep 1; done\n"


class SupervisorSlot(unittest.TestCase):
    """The real supervisor, standing by (a long grace), so only its slot is under test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-slot."))
        self.ws = self.tmp / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "state").mkdir()
        (self.tmp / "link").symlink_to(self.ws)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "tmux").write_text(TMUX_STUB)
        (self.bin / "tmux").chmod(0o755)
        self.notifier = self.tmp / "idle-notifier.sh"
        self.notifier.write_text(IDLE_NOTIFIER)
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.procs:
            kill_quietly(p.pid)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass
        time.sleep(0.5)  # the slot helpers see their parents gone and release
        try:
            shutil.rmtree(holder_lock.lock_dir(self.ws / "tasks"), ignore_errors=True)
        except (OSError, holder_lock.LockError):
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, inbox: Path, wait_s: int = 2) -> "tuple[subprocess.Popen, Path]":
        err = self.tmp / f"sup{len(self.procs)}.err"
        env = isolated_child_env(PATH=f"{self.bin}:/usr/bin:/bin", SUTANDO_TASKS_DIR=str(inbox),
                                 SUTANDO_WORKSPACE_DIR=str(self.ws), SUTANDO_TMUX_SOCKET=str(self.tmp / "t.sock"),
                                 SUTANDO_NOTIFIER_SCRIPT=str(self.notifier), SUTANDO_NOTIFIER_GRACE_PERIOD="600",
                                 SUTANDO_NOTIFIER_ROLE_POLL="1", SUTANDO_NOTIFIER_SLOT_WAIT_S=str(wait_s),
                                 SUTANDO_NOTIFIER_PY=sys.executable)
        with open(err, "w") as e:
            p = popen_bound(["bash", str(SUPERVISOR)], env=env, stdout=subprocess.DEVNULL, stderr=e)
        self.procs.append(p)
        return p, err

    def occupancy(self, want: str, timeout: float = 15) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen = holder_lock.occupancy(self.ws / "tasks")
            if seen == want:
                return seen
            time.sleep(0.2)
        return holder_lock.occupancy(self.ws / "tasks")

    def test_two_supervisors_on_one_inbox_leave_one_and_signal_nobody(self):
        first, _ = self.start(self.ws / "tasks")
        self.assertEqual(self.occupancy("held"), "held")
        second, err = self.start(self.tmp / "link" / "tasks", wait_s=1)  # the same inbox, another spelling
        self.assertEqual(second.wait(timeout=30), 0)
        self.assertIn("another supervisor holds", err.read_text())
        self.assertIn("standing down", err.read_text())
        time.sleep(0.5)
        self.assertIsNone(first.poll(), "the holder is never signalled")

    def test_a_waiter_takes_the_slot_when_the_holder_exits(self):
        first, _ = self.start(self.ws / "tasks")
        self.assertEqual(self.occupancy("held"), "held")
        waiter, err = self.start(Path(str(self.ws / "tasks") + "/"), wait_s=20)
        time.sleep(1.5)
        os.kill(first.pid, signal.SIGTERM)
        first.wait(timeout=15)
        time.sleep(4)
        self.assertIsNone(waiter.poll(), err.read_text())
        self.assertEqual(self.occupancy("held"), "held")
        third, err3 = self.start(self.ws / "tasks", wait_s=1)
        self.assertEqual(third.wait(timeout=30), 0)
        self.assertIn("standing down", err3.read_text())


class HelperScript(unittest.TestCase):
    def setUp(self):
        top = Path(tempfile.mkdtemp(prefix="hcg."))
        self.addCleanup(shutil.rmtree, top, True)
        self.root = top / "Application Support" / "sutando"
        for rel in ("src/agent/claude/cli/task-notifier.sh", "src/watch-tasks-stream.sh", "skills/a/run.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("")

    def test_the_script_operand_under_a_path_with_spaces(self):
        self.assertEqual(hcg.helper_script(f"bash {self.root}/src/agent/claude/cli/task-notifier.sh", self.root),
                         "src/agent/claude/cli/task-notifier.sh")
        py = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
        self.assertEqual(hcg.helper_script(f"{py} -u {self.root}/skills/a/run.py --x 1", self.root), "skills/a/run.py")
        self.assertEqual(hcg.helper_script(f"/usr/bin/python3.12 -W ignore {self.root}/skills/a/run.py", self.root),
                         "skills/a/run.py")

    def test_a_relative_launch_is_read_against_the_repo(self):
        self.assertEqual(hcg.helper_script("bash src/watch-tasks-stream.sh /x/tasks --role session", self.root),
                         "src/watch-tasks-stream.sh")

    def test_an_option_value_or_data_path_is_never_the_script(self):
        for cmd in (f"node /outside/worker.js --plugin-dir {self.root}/skills/a",
                    f"claude --plugin-dir {self.root}/skills/a", f"bash -c 'cat {self.root}/src/watch-tasks-stream.sh'",
                    f"python3 -m pytest {self.root}/skills/a/run.py", f"python3 -c 'print(1)' {self.root}/skills/a/run.py",
                    f"bash /elsewhere/x.sh {self.root}/src/watch-tasks-stream.sh", "", "bash"):
            self.assertIsNone(hcg.helper_script(cmd, self.root), cmd)

    def test_clock_parsing(self):
        self.assertEqual(hcg.parse_clock("01:02"), 62)
        self.assertEqual(hcg.parse_clock("1:00:00"), 3600)
        self.assertEqual(hcg.parse_clock("2-00:00:01"), 172801)
        self.assertAlmostEqual(hcg.parse_clock("0:12.50"), 12.5)
        for bad in ("", "x:y", "a-01:00", "1:2:3:4"):
            self.assertIsNone(hcg.parse_clock(bad))


START = "Sun Nov  1 16:49:01 2026"


def _concurrent_writer(state, repo, pid, barrier):
    barrier.wait()
    hcg.evaluate([{"pid": pid, "start": START, "cpu": 1.0, "command": f"bash {repo}/src/x.sh"}],
                 Path(repo), Path(state), now=1000.0)


class HelperCpu(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="hcg-repo."))
        (self.repo / "src").mkdir()
        (self.repo / "src" / "x.sh").write_text("")
        self.state = self.repo / "state" / "samples.json"
        self.state.parent.mkdir()
        self.addCleanup(shutil.rmtree, self.repo, True)

    def rows(self, cpu, pid=42, start=START):
        return [{"pid": pid, "start": start, "cpu": cpu, "command": f"bash {self.repo}/src/x.sh"}]

    def test_a_helper_hot_for_the_whole_window_is_named(self):
        self.assertEqual(hcg.evaluate(self.rows(100), self.repo, self.state, now=10_000), [])
        self.assertEqual(hcg.evaluate(self.rows(300), self.repo, self.state, now=10_300), [],
                         "inside the window: no verdict yet, the old sample is kept")
        hot = hcg.evaluate(self.rows(100 + 0.8 * 700), self.repo, self.state, now=10_700)
        self.assertEqual(hot, [{"pid": 42, "script": "src/x.sh", "cpu_pct": 80, "minutes": 12}])

    def test_a_quiet_helper_and_a_reused_pid(self):
        hcg.evaluate(self.rows(100), self.repo, self.state, now=10_000)
        self.assertEqual(hcg.evaluate(self.rows(110), self.repo, self.state, now=10_700), [])
        hcg.evaluate(self.rows(100), self.repo, self.state, now=20_000)
        self.assertEqual(hcg.evaluate(self.rows(900, start="Sun Nov  1 17:00:00 2026"), self.repo, self.state,
                                      now=20_700), [], "the same pid with another start time starts over")

    def test_an_unseen_sample_survives_an_older_reading_then_ages_out(self):
        hcg.evaluate(self.rows(1, pid=1) + self.rows(1, pid=2), self.repo, self.state, now=1000)
        hcg.evaluate(self.rows(1, pid=1), self.repo, self.state, now=1001)
        self.assertEqual(len(json.loads(self.state.read_text())), 2)
        hcg.evaluate(self.rows(1, pid=1), self.repo, self.state, now=1000 + hcg.RETAIN_S + 1)
        self.assertEqual(list(json.loads(self.state.read_text())), [f"1:{' '.join(START.split())}"])

    def test_concurrent_checks_lose_no_sample(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(4)
        procs = [ctx.Process(target=_concurrent_writer, args=(str(self.state), str(self.repo), pid, barrier))
                 for pid in (1, 2, 3, 4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        key = " ".join(START.split())
        self.assertEqual(sorted(json.loads(self.state.read_text())), sorted(f"{p}:{key}" for p in (1, 2, 3, 4)))

    def test_the_probe_names_it_without_prescribing_a_kill(self):
        hcg.evaluate(self.rows(0), self.repo, self.state, now=time.time() - 700)

        class Out:
            returncode = 0
            stdout = f"   42 {START}  11:40.00 bash {self.repo}/src/x.sh\nnoise\n"
        r = hcg.check(self.repo, self.state, runner=lambda *a, **k: Out())
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("pid 42 src/x.sh", r["detail"])
        self.assertIn("Possibly a busy loop", r["detail"])
        self.assertNotIn("KILL", r["detail"])

    def test_unknown_states_are_said_as_such(self):
        class Failed:
            returncode, stdout = 1, ""
        r = hcg.check(self.repo, self.state, runner=lambda *a, **k: Failed())
        self.assertEqual((r["status"], r["detail"]), ("warn", "not checked: ps exited 1"))

        def boom(*a, **k):
            raise OSError("no ps")
        self.assertEqual(hcg.check(self.repo, self.state, runner=boom)["status"], "warn")
        with unittest.mock.patch.object(hcg.os, "getuid", side_effect=AttributeError):
            r = hcg.check(self.repo, self.state)
        self.assertEqual((r["status"], r["detail"]), ("ok", "not checked: no POSIX process table on this platform"))

    def test_health_check_runs_the_probe(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_helper_cpu())", src)
        self.assertIn('helper_cpu_guard.check(REPO_DIR, WORKSPACE_DIR / "state" / "helper-cpu-samples.json")', src)


if __name__ == "__main__":
    unittest.main()
