"""Cross-adapter interlock: one work dir, one live adapter of any kind.

The routine adapter, the Codex connector and the cloud-session adapter all take the work dir through
one call, `task_io.acquire_lock`, a thin owner over the package's `holder_lock`: a flock in a lock
directory outside the work dir, held for the process's life, and a holder record published at once.
So whichever starts first holds the dir and every later one, of any kind, refuses at that flock with
exit 3 naming the holder. Gated like test_task_lifecycle.py. Every wait is bounded, so a red run
fails quickly. Run:
    TASK_LIFECYCLE_CONTRACT=1 python3 packages/ag2-sparrow/tests/test_cloud_session_interlock.py
"""
import atexit
import contextlib
import io
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_TESTS = PKG_ROOT.parents[1] / "tests" / "_helpers"
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import holder_lock, task_io  # noqa: E402

from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa  # noqa: E402
from ag2_sparrow.adapters.claude_routine import routine_adapter as ra  # noqa: E402

from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402

try:
    sys.path.insert(0, str(REPO_TESTS))
    from holder_lock_contract import FirstActContract  # noqa: E402
except ImportError:  # the package outside its repository has no contract mixin
    FirstActContract = None

# Off until the owner module lands; TASK_LIFECYCLE_CONTRACT=1 turns the contract on.
CONTRACT = os.environ.get("TASK_LIFECYCLE_CONTRACT", "0") == "1"
GATE = unittest.skipUnless(CONTRACT, "the task-lifecycle contract is off (TASK_LIFECYCLE_CONTRACT=1 turns it on)")
WAIT = 10.0
# holder_lock keeps its lock dirs under the uid's home; these tests and their children use a temp home.
LOCK_HOME = tempfile.mkdtemp(prefix="sparrow-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
SET_HOME = "from ag2_sparrow import holder_lock\nholder_lock._home = lambda: %r\n" % LOCK_HOME
FAKE_CLAUDE = "#!%s\nimport sys\nsys.stdin.read()\nprint('{\"ok\": true, \"url\": \"u\"}')\n"
CLOUD = """
import sys
sys.path.insert(0, %r)
%s
from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa
sys.exit(sa.main(["--work", %r, "--session-id", "session_X", "--claude", %r, "--poll", "0.05",
                  "--idle-exit", "30"], {"PATH": "/usr/bin:/bin", "HOME": %r}))
"""
CODEX = """
import sys
sys.path.insert(0, %r)
%s
from ag2_sparrow.adapters.codex import codex_connector as cc
sys.exit(cc.main(["--server", "stub", "--work", %r, "--poll", "0.05", "--idle-exit", "30"]))
"""
ROUTINE = """
import sys
sys.path.insert(0, %r)
%s
from ag2_sparrow.adapters.claude_routine import routine_adapter as ra
sys.exit(ra.main(["--work", %r, "--trigger-id", "trig_01HOLD", "--poll", "0.05", "--idle-exit", "30"],
                 {"CLAUDE_ROUTINE_TOKEN": "t"}))
"""
RACE = """
import builtins, os, sys
sys.path.insert(0, %r)
%s
from ag2_sparrow import task_io
work, pause, home = %r, [%r], %r
real_open, real_bopen = os.open, builtins.open


def hold(path, dir_fd=None):
    # holder_lock creates in its lock dir through a dir fd; nothing else here does
    if pause[0] and (dir_fd is not None or os.path.abspath(os.fspath(path)).startswith(home + os.sep)):
        pause[0] = False
        print("opened", flush=True)
        sys.stdin.readline()


def os_open(path, flags, *a, **k):
    fd = real_open(path, flags, *a, **k)
    if flags & os.O_CREAT:
        hold(path, k.get("dir_fd"))
    return fd


def b_open(path, mode="r", *a, **k):
    f = real_bopen(path, mode, *a, **k)
    if any(c in mode for c in "wax+"):
        hold(path)
    return f


os.open, builtins.open = os_open, b_open
try:
    task_io.acquire_lock(work, "racer")
    print("acquired", os.getpid(), flush=True)
except task_io.LockRefused:
    print("refused", os.getpid(), flush=True)
sys.stdin.readline()
"""


def read_line(stream, timeout=WAIT):
    if not select.select([stream], [], [], timeout)[0]:
        raise AssertionError("no line within %.0fs" % timeout)
    return stream.readline()


def wait_for(predicate, what, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out after %.0fs waiting for %s" % (timeout, what))


def holder(work):
    """(adapter, pid) of the published holder record of `work`, or None."""
    rec = holder_lock.read_holder(str(work))
    return (rec.record["adapter"], rec.record["pid"]) if isinstance(rec, holder_lock.Valid) else None


@GATE
class InterlockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.work = self.base / "work"
        self.claude = self.base / "claude"
        self.claude.write_text(FAKE_CLAUDE % sys.executable)
        self.claude.chmod(0o755)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=WAIT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=WAIT)

    def spawn(self, code):
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def start(self, kind):
        """An adapter of `kind` holding the dir; returns (adapter name, pid) from its published record."""
        code = {"cloud": CLOUD % (str(PKG_ROOT), SET_HOME, str(self.work), str(self.claude), str(self.base)),
                "routine": ROUTINE % (str(PKG_ROOT), SET_HOME, str(self.work)),
                "codex": CODEX % (str(PKG_ROOT), SET_HOME, str(self.work))}[kind]
        child = self.spawn(code)
        legacy = self.work / "state" / "connector.pid"
        wait_for(lambda: holder(self.work) or child.poll() is not None or legacy.exists(),
                 "the %s adapter to hold the dir" % kind)
        if child.poll() is not None:
            self.fail("the %s adapter exited early: %s" % (kind, child.stderr.read()))
        if not holder(self.work):
            self.fail("the %s adapter holds the dir by a pidfile, not by the shared lock" % kind)
        return child, holder(self.work)

    def run_main(self, fn, argv, environ=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rc = fn(argv, environ) if environ is not None else fn(argv)
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def cloud_main(self):
        return self.run_main(sa.main, ["--work", str(self.work), "--session-id", "session_X", "--claude",
                                       str(self.claude), "--poll", "0.05", "--idle-exit", "0.1"],
                             {"PATH": "/usr/bin:/bin", "HOME": str(self.base)})

    def routine_main(self):
        return self.run_main(ra.main, ["--work", str(self.work), "--trigger-id", "trig_01LATE", "--poll", "0.05",
                                       "--idle-exit", "0.1"], {"CLAUDE_ROUTINE_TOKEN": "t"})

    def codex_main(self):
        return self.run_main(cc.main, ["--server", "stub", "--work", str(self.work), "--once", "1",
                                       "--idle-exit", "0.1", "--poll", "0.05"])

    def assert_refused_at_the_flock(self, result, held_by):
        rc, err = result
        self.assertEqual(rc, 3, err)
        self.assertIn("refusing to start: %s is held by %s pid %d on " % (self.work, held_by[0], held_by[1]), err)
        self.assertEqual(holder(self.work), held_by, "nobody took it over")

    def test_cloud_first_then_routine_then_codex_refuse(self):
        _, held_by = self.start("cloud")
        self.assertEqual(held_by[0], "claude-cloud-session")
        self.assert_refused_at_the_flock(self.routine_main(), held_by)
        self.assert_refused_at_the_flock(self.codex_main(), held_by)
        self.assert_refused_at_the_flock(self.cloud_main(), held_by)

    def test_routine_first_then_cloud_refuses_at_the_flock(self):
        _, held_by = self.start("routine")
        before = sorted(os.listdir(self.work))
        self.assert_refused_at_the_flock(self.cloud_main(), held_by)
        self.assertEqual(sorted(os.listdir(self.work)), before, "the refused adapter created nothing")

    def test_the_cloud_holds_the_flock_and_a_routine_refuses_at_it(self):
        _, held_by = self.start("cloud")
        before = sorted(os.listdir(self.work / "state"))
        self.assert_refused_at_the_flock(self.routine_main(), held_by)
        self.assertEqual(sorted(os.listdir(self.work / "state")), before, "the refused routine wrote nothing")

    def test_codex_first_then_cloud_refuses(self):
        _, held_by = self.start("codex")
        self.assert_refused_at_the_flock(self.cloud_main(), held_by)

    def test_a_live_legacy_pid_refuses_and_explains(self):
        live = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (live.kill(), live.wait(WAIT)))
        (self.work / "state").mkdir(parents=True)
        (self.work / "state" / "connector.pid").write_text("%d\n" % live.pid)
        for name, run in (("cloud", self.cloud_main), ("routine", self.routine_main), ("codex", self.codex_main)):
            with self.subTest(adapter=name):
                rc, err = run()
                self.assertEqual(rc, 3, err)
                self.assertIn("stop the old adapter (pid %d), then start" % live.pid, err)
                self.assertIsNone(holder(self.work), "refused before publishing")

    def test_a_malformed_legacy_pidfile_refuses_naming_the_file(self):
        (self.work / "state").mkdir(parents=True)
        path = self.work / "state" / "connector.pid"
        path.write_text("")
        rc, err = self.cloud_main()
        self.assertEqual(rc, 3, err)
        self.assertIn("%s is not a pid record; stop the old adapter and remove it, then start" % path, err)
        self.assertEqual(path.read_text(), "", "never written or removed by the lock")

    def run_with_the_root_replaced_after_acquired(self, run):
        """The path is renamed away and a new directory put there right after Acquired: the adapter
        acts on the inode it holds, and the new directory stays untouched."""
        self.work.mkdir(parents=True)
        real, moved = holder_lock.acquire, self.base / "moved"

        def acquire_then_replace(*a, **k):
            got = real(*a, **k)
            if isinstance(got, holder_lock.Acquired):
                os.rename(self.work, moved)
                self.work.mkdir()
            return got
        with mock.patch.object(holder_lock, "acquire", acquire_then_replace):
            rc, err = run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.listdir(self.work), [], "the new directory at the path is untouched")
        self.assertTrue({"tasks", "results", "state"} <= set(os.listdir(moved)), "writes land in the original")

    def test_the_routine_adapter_acts_on_its_root_inode(self):
        self.run_with_the_root_replaced_after_acquired(self.routine_main)

    def test_the_codex_adapter_acts_on_its_root_inode(self):
        self.run_with_the_root_replaced_after_acquired(self.codex_main)

    def test_the_cloud_adapter_acts_on_its_root_inode(self):
        self.run_with_the_root_replaced_after_acquired(self.cloud_main)
        self.assertTrue((self.base / "moved" / "state" / "format").exists(), "first init on the original too")

    def test_sigterm_runs_release_and_leaves_no_record(self):
        for kind in ("cloud", "routine", "codex"):
            with self.subTest(adapter=kind):
                self.work = self.base / kind
                child, held_by = self.start(kind)
                os.kill(held_by[1], signal.SIGTERM)
                child.wait(WAIT)
                self.assertEqual(child.returncode, 128 + signal.SIGTERM, child.stderr.read())
                self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing,
                                      "release ran: the record is gone")
                self.assertFalse((self.work / "state" / "connector.pid").exists())
                task_io.release_lock(task_io.acquire_lock(str(self.work), "check"))


@GATE
class AcquireLockTest(unittest.TestCase):
    """The shared lock call every adapter uses (task_io.acquire_lock), raced for real."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "work"
        self.children = []
        self.addCleanup(self.stop)

    def stop(self):
        for child in self.children:
            with contextlib.suppress(OSError):
                child.stdin.close()
            try:
                child.wait(timeout=WAIT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=WAIT)

    def racer(self, pause):
        child = subprocess.Popen([sys.executable, "-c", RACE % (str(PKG_ROOT), SET_HOME, str(self.work), pause,
                                                                LOCK_HOME)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def test_a_racer_paused_between_open_and_write_never_lets_both_acquire(self):
        first = self.racer(True)
        self.assertEqual(read_line(first.stdout).strip(), "opened", "the first racer paused after its open")
        second = self.racer(False)
        second_says = read_line(second.stdout).split()
        first.stdin.write("\n")
        first.stdin.flush()
        first_says = read_line(first.stdout).split()
        outcomes = sorted([first_says[0], second_says[0]])
        self.assertEqual(outcomes, ["acquired", "refused"], (first_says, second_says))
        winner = first_says if first_says[0] == "acquired" else second_says
        self.assertEqual(holder(self.work), ("racer", int(winner[1])))


RUNNER = PKG_ROOT / "tests" / "first_act_runner.py"


def first_act_case(adapter, name):
    """The parent's FirstActContract against one adapter's real CLI (through first_act_runner.py)."""
    if FirstActContract is None:
        return unittest.skip("the repository's holder_lock contract mixin is not here")(type(
            "Skipped", (unittest.TestCase,), {}))

    class Case(FirstActContract, unittest.TestCase):
        module, module_kind = holder_lock, "vendored"

        def caller(self, root, *extra):
            return [str(RUNNER), adapter, root, LOCK_HOME] + list(extra)
    Case.__name__ = Case.__qualname__ = name
    return GATE(Case)


RoutineFirstActTest = first_act_case("routine", "RoutineFirstActTest")
CodexFirstActTest = first_act_case("codex", "CodexFirstActTest")
CloudFirstActTest = first_act_case("cloud", "CloudFirstActTest")


if __name__ == "__main__":
    unittest.main()
