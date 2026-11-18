"""Cross-adapter interlock: one work dir, one live adapter of any kind.

The routine adapter, the Codex connector and the cloud-session adapter all take the work dir through
one call, `task_io.acquire_lock`, a thin owner over the package's `holder_lock`: a flock in a lock
directory outside the work dir, held for the process's life, and a holder record published at once.
So whichever starts first holds the dir and every later one, of any kind, refuses at that flock with
exit 3 naming the holder. Gated like test_task_lifecycle.py. Every wait is bounded, so a red run
fails quickly. Run:
    python3 packages/ag2-sparrow/tests/test_cloud_session_interlock.py
"""
import atexit
import contextlib
import importlib.util
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

# On now that the owner module has landed; TASK_LIFECYCLE_CONTRACT=0 turns the contract off.
CONTRACT = os.environ.get("TASK_LIFECYCLE_CONTRACT", "1") == "1"
GATE = unittest.skipUnless(CONTRACT, "the task-lifecycle contract is off (TASK_LIFECYCLE_CONTRACT=0)")
WAIT = 10.0
# holder_lock keeps its lock dirs under the uid's home; these tests and their children use a temp home.
LOCK_HOME = tempfile.mkdtemp(prefix="sparrow-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
SET_HOME = "from ag2_sparrow import holder_lock\nholder_lock._home = lambda: %r\n" % LOCK_HOME
# Logs every holder_lock call (and whether a SIGTERM handler is on its stack), the signal, and each
# stop check the serve loops make, one line each, to the given file.
SPY = """
import sys, time
from ag2_sparrow import holder_lock as _hl, relay_loop as _rl, task_io as _tio
from ag2_sparrow.adapters.codex import codex_connector as _cc
_log, _handler = open(%r, "a", buffering=1), _tio._terminated


def _note(*words):
    _log.write(" ".join(map(str, words)) + " %%.4f\\n" %% time.monotonic())


def _in_handler():
    f = sys._getframe(1)
    while f is not None and f.f_code not in (_handler.__code__, _spy_handler.__code__):
        f = f.f_back
    return f is not None


def _spy(name, fn):
    return lambda *a, **k: (_note("handler" if _in_handler() else "main", name), fn(*a, **k))[1]


def _spy_handler(signum, frame):
    _note("signal", signum)
    _handler(signum, frame)


def _check(fn):
    return lambda: (lambda got: (_note("check", got), got)[1])(fn())


for _name in [n for n in dir(_hl) if not n.startswith("_")]:
    _fn = getattr(_hl, _name)
    if callable(_fn) and not isinstance(_fn, type):
        setattr(_hl, _name, _spy(_name, _fn))
_tio._terminated = _spy_handler
_rl.stop_requested = _check(_rl.stop_requested)
_cc.stop_requested = _check(_cc.stop_requested)
"""
# After SPY: the relay loop always has one task whose retry is 30 s away, at a 1 s poll (cap 50 s).
LONG_BACKOFF = """
_serve = _rl.RelayLoop.serve


def _backoff_serve(self):
    self.args.poll = 1.0
    self.pending = lambda seen: ["task-later.txt"]
    self.next_ready = lambda names: (_note("backoff", 30), (None, 30))[1]
    return _serve(self)


_rl.RelayLoop.serve = _backoff_serve
"""
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


def spied(log):
    """The SPY log as (words, monotonic time) pairs."""
    return [(w, float(t)) for w, t in (line.rsplit(" ", 1) for line in log.read_text().splitlines())]


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

    def start(self, kind, spy=""):
        """An adapter of `kind` holding the dir; returns (adapter name, pid) from its published record."""
        setup = SET_HOME + spy
        code = {"cloud": CLOUD % (str(PKG_ROOT), setup, str(self.work), str(self.claude), str(self.base)),
                "routine": ROUTINE % (str(PKG_ROOT), setup, str(self.work)),
                "codex": CODEX % (str(PKG_ROOT), setup, str(self.work))}[kind]
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

    def serving(self, kind):
        """Start an adapter and wait until its serve loop runs its first stop check: every startup
        step (directories, probes, first init) is over by then."""
        log = self.base / ("%s.ready" % kind)
        started = self.start(kind, SPY % str(log))
        wait_for(lambda: log.exists() and "check 0" in [w for w, _ in spied(log)],
                 "the %s serve loop to start" % kind)
        return started

    def test_cloud_first_then_routine_then_codex_refuse(self):
        _, held_by = self.start("cloud")
        self.assertEqual(held_by[0], "claude-cloud-session")
        self.assert_refused_at_the_flock(self.routine_main(), held_by)
        self.assert_refused_at_the_flock(self.codex_main(), held_by)
        self.assert_refused_at_the_flock(self.cloud_main(), held_by)

    def test_routine_first_then_cloud_refuses_at_the_flock(self):
        _, held_by = self.serving("routine")
        before = sorted(os.listdir(self.work))
        self.assert_refused_at_the_flock(self.cloud_main(), held_by)
        self.assertEqual(sorted(os.listdir(self.work)), before, "the refused adapter created nothing")

    def test_the_cloud_holds_the_flock_and_a_routine_refuses_at_it(self):
        _, held_by = self.serving("cloud")
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

    def test_sigterm_only_flags_and_the_loop_releases_within_one_step(self):
        """The handler calls nothing on holder_lock; the serve loop's next stop check sees the flag,
        and release follows it with no further loop step."""
        for kind in ("cloud", "routine", "codex"):
            with self.subTest(adapter=kind):
                self.work = self.base / kind
                log = self.base / ("%s.spy" % kind)
                child, held_by = self.start(kind, SPY % str(log))
                wait_for(lambda: log.exists() and "check 0" in [w for w, _ in spied(log)],
                         "the %s serve loop to run" % kind)
                os.kill(held_by[1], signal.SIGTERM)
                child.wait(WAIT)
                self.assertEqual(child.returncode, 128 + signal.SIGTERM, child.stderr.read())
                lines = spied(log)
                self.assertEqual([w for w, _ in lines if w.startswith("handler ")], [],
                                 "the signal handler called holder_lock")
                at = [w for w, _ in lines].index("signal %d" % signal.SIGTERM)
                after = [(w, t) for w, t in lines[at + 1:] if w != "main read_holder"]
                self.assertEqual([w for w, _ in after[:2]], ["check %d" % signal.SIGTERM, "main release"],
                                 "the next loop step stops, and release comes from the loop's caller")
                self.assertLess(after[1][1] - lines[at][1], 1.0, "release within one 0.05s step")
                self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing,
                                      "release ran: the record is gone")
                self.assertFalse((self.work / "state" / "connector.pid").exists())
                task_io.release_lock(task_io.acquire_lock(str(self.work), "check"))

    def test_sigterm_during_a_long_retry_backoff_releases_within_two_seconds(self):
        """The backoff sleep wakes on the stop flag; a running provider turn is bounded by its own
        timeout instead (not driven here)."""
        for kind in ("cloud", "routine"):
            with self.subTest(adapter=kind):
                self.work = self.base / kind
                log = self.base / ("%s.backoff" % kind)
                child, held_by = self.start(kind, SPY % str(log) + LONG_BACKOFF)
                wait_for(lambda: log.exists() and "backoff 30" in [w for w, _ in spied(log)],
                         "the %s loop to enter its 30 s backoff" % kind)
                time.sleep(0.3)
                os.kill(held_by[1], signal.SIGTERM)
                child.wait(WAIT)
                self.assertEqual(child.returncode, 128 + signal.SIGTERM, child.stderr.read())
                lines = spied(log)
                at = [w for w, _ in lines].index("signal %d" % signal.SIGTERM)
                released = [t for w, t in lines[at + 1:] if w == "main release"]
                self.assertTrue(released, "release ran after the signal")
                self.assertLess(released[0] - lines[at][1], 2.0, "release within 2 s, not after the backoff")
                self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing)


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


class TaskIoBranchTest(unittest.TestCase):
    """task_io's refusal, path-resolution, stop-flag and legacy-pidfile branches, in this process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "work"
        (self.work / "state").mkdir(parents=True)

    def owner(self):
        got = holder_lock.acquire(str(self.work), install_id=holder_lock.install_id(), timeout=0)
        self.assertIsInstance(got, holder_lock.Acquired)
        self.addCleanup(task_io.release_lock, got.owner)
        return got.owner

    def refused(self, outcome=None, error=None, **kw):
        with mock.patch.object(holder_lock, "acquire", return_value=outcome, side_effect=error):
            with self.assertRaises(task_io.LockRefused) as cm:
                task_io.acquire_lock("/w", "x")
        return cm.exception

    def test_every_non_acquired_outcome_is_a_refusal_with_its_code(self):
        valid = holder_lock.Valid({"adapter": "codex", "pid": 7, "host": "h"})
        e = self.refused(holder_lock.Held(valid, "it is running"))
        self.assertEqual((e.code, "codex pid 7 on h (it is running)" in e.line), (3, True))
        e = self.refused(holder_lock.Contended(holder_lock.Invalid("torn", b"")))
        self.assertEqual((e.code, "record is unreadable" in e.line), (3, True))
        e = self.refused(holder_lock.LegacyRunning(42, "/w/state/connector.pid"))
        self.assertEqual((e.code, "old adapter (pid 42)" in e.line), (3, True))
        e = self.refused(holder_lock.LegacyRunning(None, "/w/state/connector.pid"))
        self.assertEqual((e.code, "is not a pid record" in e.line), (3, True))
        e = self.refused(holder_lock.Unreadable("EIO"))
        self.assertEqual((e.code, e.line.endswith("EIO")), (2, True))
        e = self.refused(error=holder_lock.LockError("replaced"))
        self.assertEqual((e.code, e.line.endswith("replaced")), (2, True))

    def test_a_publish_failure_releases_the_lock_and_refuses(self):
        owner = object()
        with mock.patch.object(holder_lock, "acquire", return_value=holder_lock.Acquired(owner, holder_lock.Missing())), \
                mock.patch.object(holder_lock, "publish", side_effect=holder_lock.LockError("full")), \
                mock.patch.object(holder_lock, "release") as release:
            with self.assertRaises(task_io.LockRefused) as cm:
                task_io.acquire_lock("/w", "x")
        self.assertEqual(cm.exception.code, 2)
        release.assert_called_once_with(owner)

    def test_a_release_that_fails_is_swallowed(self):
        with mock.patch.object(holder_lock, "release", side_effect=holder_lock.LockError("gone")):
            task_io.release_lock(object())

    def test_root_path_follows_a_replaced_path_or_refuses(self):
        owner = self.owner()
        found = task_io.root_path(owner, str(self.work.parent / "elsewhere"))
        self.assertTrue(os.path.samefile(found, self.work))
        # On a real Linux /proc resolves the fd; the refusal is the branch where it cannot.
        def readlink_fails(*a, **k):
            raise FileNotFoundError("/proc/self/fd")
        with mock.patch.object(task_io.sys, "platform", "linux"), \
                mock.patch.object(task_io.os, "readlink", readlink_fails):
            with self.assertRaises(task_io.LockRefused) as cm:
                task_io.root_path(owner, str(self.work.parent / "elsewhere"))
        self.assertIn("cannot resolve the held work root", cm.exception.line)
        real = os.stat
        calls = []

        def stat(path, *a, **k):
            calls.append(path)
            if len(calls) == 1:
                raise FileNotFoundError(path)
            st = real(path, *a, **k)
            return os.stat_result((st.st_mode, st.st_ino + 1, st.st_dev) + tuple(st)[3:])
        with mock.patch.object(task_io.os, "stat", stat):
            with self.assertRaises(task_io.LockRefused) as cm:
                task_io.root_path(owner, "/nonexistent")
        self.assertIn("no reachable path", cm.exception.line)

    def test_the_stop_flag_is_set_by_the_handler_and_names_the_exit_code(self):
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        self.addCleanup(task_io._STOP.__setitem__, 0, 0)
        task_io.exit_on_sigterm()
        self.assertIs(signal.getsignal(signal.SIGTERM), task_io._terminated)
        self.assertEqual((task_io.stop_requested(), task_io.exit_code(0)), (0, 0))
        signal.raise_signal(signal.SIGTERM)
        self.assertEqual((task_io.stop_requested(), task_io.exit_code(0)), (signal.SIGTERM, 128 + signal.SIGTERM))
        start = time.monotonic()
        task_io.stoppable_sleep(5)
        self.assertLess(time.monotonic() - start, 1, "a requested stop ends the sleep")

    def test_the_legacy_pidfile_is_removed_only_when_a_dead_adapter_wrote_it_before_this_start(self):
        owner = self.owner()
        pidfile = self.work / "state" / "connector.pid"
        self.assertIsNone(task_io.clear_attributed_pidfile(owner, str(self.work)))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        old = time.time() - 1000

        def write(text):
            pidfile.write_text(text)
            os.utime(pidfile, (old, old))
        write("%d\n" % dead.pid)
        self.assertIsNone(task_io.clear_attributed_pidfile(owner, str(self.work)))
        self.assertFalse(pidfile.exists(), "removed once")
        for text in ("%d\n" % os.getpid(), "not a pid", "12 extra\n"):
            write(text)
            self.assertIn("not a dead adapter's pidfile", task_io.clear_attributed_pidfile(owner, str(self.work)))
            self.assertTrue(pidfile.exists())
        # The module's own start line runs under a frozen clock, so what it stores is what is compared.
        spec = importlib.util.spec_from_file_location("ag2_sparrow._task_io_frozen", task_io.__file__)
        fresh = importlib.util.module_from_spec(spec)
        with mock.patch.object(time, "time", return_value=1000.9):
            spec.loader.exec_module(fresh)
        self.assertEqual(fresh._started_at(), 1000.9)
        for mtime, removed in ((1000.899, True), (1000.901, False)):
            write("%d\n" % dead.pid)
            os.utime(pidfile, (mtime, mtime))
            got = fresh.clear_attributed_pidfile(owner, str(self.work))
            self.assertEqual((got is None, pidfile.exists()), (removed, not removed),
                             "mtime %.3f, start 1000.900 (same whole second)" % mtime)
        write("%d\n" % dead.pid)
        os.utime(pidfile, None)
        self.assertIn("not a dead adapter's pidfile", task_io.clear_attributed_pidfile(owner, str(self.work)),
                      "written after this process started")
        write("%d\n" % dead.pid)
        with mock.patch.object(task_io.os, "unlink", side_effect=PermissionError(1, "no")):
            self.assertIn("cannot remove", task_io.clear_attributed_pidfile(owner, str(self.work)))
        pidfile.unlink()
        pidfile.mkdir()
        self.assertIn("cannot read", task_io.clear_attributed_pidfile(owner, str(self.work)))

    def test_every_adapter_installs_the_stop_handler_before_it_takes_the_lock(self):
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        for mod, argv, env in ((sa, ["--session-id", "session_x", "--claude", sys.executable], {"PATH": "/usr/bin:/bin"}),
                               (ra, ["--trigger-id", "trig_x"], {"CLAUDE_ROUTINE_TOKEN": "t"}),
                               (cc, ["--server", "stub"], None)):
            with self.subTest(adapter=mod.__name__):
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                seen = []

                def acquire(*a, **k):
                    seen.append(signal.getsignal(signal.SIGTERM))
                    raise task_io.LockRefused(3, "refusing to start: held")
                args = ["--work", str(self.work)] + argv
                with mock.patch.object(mod, "acquire_lock", acquire), contextlib.redirect_stderr(io.StringIO()):
                    rc = mod.main(args, env) if env is not None else mod.main(args)
                self.assertEqual((rc, seen), (3, [task_io._terminated]))

    def test_the_module_prints_the_note_and_asks_for_a_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(task_io.main(["--non-owner-note"]), 0)
        self.assertEqual(out.getvalue().rstrip("\n"), task_io.NON_OWNER_NOTE.rstrip("\n"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            task_io.main([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
