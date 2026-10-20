"""The task-lifecycle owner (`src/task_lifecycle.py`, vendored as `ag2_sparrow/task_lifecycle.py`).

Every case runs against the vendored copy, and again against the canonical src/ copy when this
package sits in its repository. Run:
    python3 packages/ag2-sparrow/tests/test_task_lifecycle.py
"""
import ast
import atexit
import select
import signal
import stat
import random
import re
import shlex
import shutil
import contextlib
import errno
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_SRC = PKG_ROOT.parents[1] / "src"
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import holder_lock, result_disposal, result_ready, task_io  # noqa: E402

from ag2_sparrow import task_lifecycle as vendored  # noqa: E402

from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa  # noqa: E402
from ag2_sparrow.adapters.claude_routine import routine_adapter as ra  # noqa: E402

from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402

ADAPTERS = PKG_ROOT / "ag2_sparrow" / "adapters"
# holder_lock keeps its lock dirs under the uid's home; these tests point it at a temp home instead.
LOCK_HOME = tempfile.mkdtemp(prefix="sparrow-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
SET_HOME = "from ag2_sparrow import holder_lock\nholder_lock._home = lambda: %r\n" % LOCK_HOME
# On now that the owner module has landed; TASK_LIFECYCLE_CONTRACT=0 turns the contract off.
CONTRACT = os.environ.get("TASK_LIFECYCLE_CONTRACT", "1") == "1"
GATE = unittest.skipUnless(CONTRACT, "the task-lifecycle contract is off (TASK_LIFECYCLE_CONTRACT=0)")
LEGACY = ("state/connector.pid", "state/sends.json", "state/fires.json", "state/sending")


def src_copy():
    if not (REPO_SRC / "task_lifecycle.py").is_file():
        return None
    spec = importlib.util.spec_from_file_location("task_lifecycle_src", REPO_SRC / "task_lifecycle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def quiet(*a):
    pass


def fd_path(fd):
    try:
        if sys.platform == "darwin":
            import fcntl
            return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return ""


def fd_name(fd):
    """The name of the file an fd refers to; a dot-temp shows as <temp>."""
    name = os.path.basename(fd_path(fd)) or "?"
    return "<temp>" if name.startswith(".") and name.endswith(".tmp") else name


def dir_label(fd):
    """A directory fsync by the identity of the descriptor fsync received: its name only when the
    path it reports is that same (st_dev, st_ino); otherwise a distinct unknown label."""
    st = os.fstat(fd)
    path = fd_path(fd)
    try:
        here = os.stat(path)
    except OSError:
        here = None
    if here is not None and (here.st_dev, here.st_ino) == (st.st_dev, st.st_ino):
        return os.path.basename(path)
    return "?%d:%d" % (st.st_dev, st.st_ino)


def durable_events(lc, events):
    """Patches recording each fsync (a directory by descriptor identity), rename and no-replace rename."""
    real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename
    real_move = lc.undelivered_quarantine.move_private

    def fsync(fd):
        events.append(("dir " + dir_label(fd)) if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync " + fd_name(fd))
        return real_fsync(fd)

    def renamed(real):
        return lambda a, b, **k: (events.append("replace " + Path(b).name), real(a, b, **k))[1]
    return [mock.patch.object(lc.os, "fsync", fsync),
            mock.patch.object(lc.os, "replace", renamed(real_replace)),
            mock.patch.object(lc.os, "rename", renamed(real_rename)),
            mock.patch.object(lc.undelivered_quarantine, "move_private",
                              lambda a, b, log=None: (events.append("final " + Path(b).name),
                                                      real_move(a, b, log))[1])]


class Base(unittest.TestCase):
    lc = vendored

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.addCleanup(self.drop_lock_dir)
        self.clock = Clock()

    def drop_lock_dir(self):
        """The lock directory is keyed by the root's inode, which a Linux tmpfs/ext4 reuses: a record
        or junk left by one test must not be inherited by the next fresh temp dir."""
        try:
            shutil.rmtree(holder_lock.lock_dir(str(self.work)), True)
        except OSError:
            pass

    def life(self):
        return self.lc.Lifecycle(self.work, clock=self.clock, log=quiet)

    def task(self, tid="task-a", text="hi"):
        p = self.work / "tasks" / (tid + ".txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("id: %s\nchannel_id: !room:ag2.space\naccess_tier: owner\ntask: %s\n" % (tid, text))
        return p

    @property
    def results(self):
        return self.work / "results"

    def deny(self, path):
        """Make `path` unreadable; skipped where permissions are not enforced (root)."""
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o700)
        if os.access(path, os.R_OK):
            self.skipTest("permissions are not enforced for this user")

    def calls(self):
        p = Path(self.tmp.name + "-bin") / "claude.calls"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def fake_claude(self):
        bindir = Path(self.tmp.name + "-bin")
        claude = bindir / "claude"
        if not claude.exists():
            bindir.mkdir(exist_ok=True)
            self.addCleanup(shutil.rmtree, bindir, True)
            claude.write_text(FAKE_CLAUDE % sys.executable)
            claude.chmod(0o755)
        return claude

    def main(self, work=None, environ=None, extra=()):
        """The cloud-session adapter's main on `work`, with a fake claude; (rc, stderr)."""
        claude = self.fake_claude()
        bindir = claude.parent
        err = io.StringIO()
        env = environ or {"PATH": os.environ.get("PATH", ""), "HOME": str(bindir)}
        with contextlib.redirect_stderr(err):
            rc = sa.main(["--work", str(work or self.work), "--session-id", "session_X", "--claude", str(claude),
                          "--idle-exit", "0.01", "--poll", "0.01"] + list(extra), env)
        return rc, err.getvalue()

    def main_in_a_new_process(self, work=None):
        """main in a process started now, so "before this start" is measured from here."""
        self.fake_claude()
        code = ("import sys\nsys.path.insert(0, %r)\n%s"
                "from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa\n"
                "sys.exit(sa.main(['--work', %r, '--session-id', 'session_X', '--claude', %r, '--idle-exit', '0.2',"
                " '--poll', '0.02'], {'PATH': '/usr/bin:/bin', 'HOME': %r}))\n"
                % (str(PKG_ROOT), SET_HOME, str(work or self.work), self.tmp.name + "-bin/claude", self.tmp.name))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        return r.returncode, r.stderr

    @contextlib.contextmanager
    def flock_elsewhere(self, work=None):
        """The work dir's holder_lock flock held through another open file description."""
        lock = Path(holder_lock.lock_dir(str(work or self.work))) / "lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertTrue(holder_lock.file_lock.try_lock_fd(fd))
            yield lock.parent
        finally:
            os.close(fd)

    def run_to_outcome(self, life, tid="task-a", kind="queued", body="[no-send]\n"):
        path = self.task(tid)
        attempt = life.prepare(tid, "interrupted\n")
        self.assertTrue(life.begin_call(tid, attempt, path))
        life.record_outcome(tid, kind, body)
        return attempt


# ── the lock: a thin owner over holder_lock (lock note v6) ─────────────────

HOLD = r"""
import sys, time
sys.path.insert(0, %r)
%s
from ag2_sparrow import task_io
owner = task_io.acquire_lock(%r, "holder-test")
print("held", flush=True)
time.sleep(30)
"""

# The prior release's pidfile writer, verbatim (codex_connector.acquire_lock before holder_lock).
PRIOR_WRITER = r"""
import os, sys, time


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(state_dir):
    path = os.path.join(state_dir, "connector.pid")
    for _ in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as f:
                    other = int(f.read().strip() or 0)
            except (OSError, ValueError):
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                return None
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%%d\n" %% os.getpid())
        return path
    return None


lock = acquire_lock(%r)
try:
    print("held", os.getpid(), flush=True)
    time.sleep(30)
finally:
    os.unlink(lock)
"""


def read_line(child, timeout=10.0):
    """One line from a child's stdout, or AssertionError after `timeout`: a red run never hangs."""
    ready, _, _ = select.select([child.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("no line from the child within %.0fs" % timeout)
    return child.stdout.readline()


class LockTest(Base):
    def holder(self):
        child = subprocess.Popen([sys.executable, "-c", HOLD % (str(PKG_ROOT), SET_HOME, str(self.work))],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(10)))
        self.assertEqual(read_line(child).strip(), "held", "the holder took the lock (EOF means it died)")
        return child

    def test_one_holder_then_exit_3_naming_it_then_admitted_when_it_dies(self):
        child = self.holder()
        pid = holder_lock.read_holder(str(self.work)).record["pid"]
        rc, err = self.main()
        self.assertEqual(rc, 3, err)
        self.assertIn("refusing to start: %s is held by holder-test pid %d on %s ("
                      % (self.work, pid, holder_lock.socket.gethostname()), err)
        child.kill()
        child.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and task_io.pid_alive(pid):
            time.sleep(0.02)
        rc, err = self.main()
        self.assertEqual(rc, 0, err)
        self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing, "released on exit")

    def test_an_unreadable_holder_record_gets_the_generic_line(self):
        with self.flock_elsewhere() as d:
            (d / "holder").write_text("{torn")
            rc, err = self.main()
        self.assertEqual(rc, 3)
        self.assertIn("refusing to start: %s is held by a process whose record is unreadable" % self.work, err)

    def test_other_lock_failures_are_exit_2(self):
        d = Path(holder_lock.lock_dir(str(self.work)))
        d.mkdir(parents=True)
        (d / "lock").mkdir()
        rc, err = self.main()
        self.assertEqual(rc, 2, err)
        os.rmdir(d / "lock")
        with mock.patch.object(holder_lock, "_statfs_type", return_value="nfs"):
            rc, err = self.main()
        self.assertEqual(rc, 2)
        self.assertIn("not a local filesystem", err)
        (d / "holder").mkdir()
        rc, err = self.main()
        self.assertEqual(rc, 2, "an unreadable record: %s" % err)
        self.assertEqual(os.listdir(self.work), [], "a refused process creates nothing under the root")

    def test_a_replaced_lock_file_or_a_changed_root_is_refused(self):
        real = holder_lock.file_lock.try_lock_fd
        lock = Path(holder_lock.lock_dir(str(self.work))) / "lock"

        def replace_then_lock(fd):
            (lock.parent / "fresh").write_text("")
            os.replace(lock.parent / "fresh", lock)
            return real(fd)
        with mock.patch.object(holder_lock.file_lock, "try_lock_fd", replace_then_lock):
            rc, err = self.main()
        self.assertEqual(rc, 2)
        self.assertIn("replaced", err)
        root = self.work / "root"
        root.mkdir()

        def swap_then_lock(fd):
            os.rename(root, self.work / "moved")
            os.symlink(self.work / "moved", root)
            return real(fd)
        with mock.patch.object(holder_lock.file_lock, "try_lock_fd", swap_then_lock):
            rc, err = self.main(root)
        self.assertEqual(rc, 2)
        self.assertIn("root changed", err)

    def test_lock_held_is_holder_locks_occupancy(self):
        """task_io.lock_held delegates to the one tri-state reader; unobserved reads as unknown."""
        self.assertEqual(task_io.lock_held(str(self.work)), "free")
        with mock.patch.object(holder_lock, "occupancy", return_value="unobserved") as occ:
            self.assertEqual(task_io.lock_held(str(self.work)), "unknown")
        occ.assert_called_once_with(str(self.work))
        with mock.patch.object(holder_lock, "occupancy", side_effect=holder_lock.LockError("x")):
            self.assertEqual(task_io.lock_held(str(self.work)), "unknown")
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (child.kill(), child.wait(10)))
        start = holder_lock.start_time(child.pid)
        self.work.mkdir(exist_ok=True)
        lock_dir = Path(holder_lock.lock_dir(str(self.work)))
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").touch()
        (lock_dir / "holder").write_bytes(json.dumps({
            "schema": 2, "adapter": "claude-local", "pid": child.pid, "start": start, "host": holder_lock._host(),
            "install_id": "i", "lock": {"dev": 1, "ino": 2}, "acquired_at": 1.0, "owner_pid": 2 ** 22 - 1,
            "owner_start": "2000-01-01T00:00:00Z", "consumer": {"pid": child.pid, "start": start},
            "tombstone": None}).encode())
        self.assertEqual(task_io.lock_held(str(self.work)), "held", "a free flock, a live consumer: the sticky rule")

    def test_the_lock_is_the_first_act(self):
        self.holder()
        before = sorted(os.listdir(self.work))
        rc, err = self.main()
        self.assertEqual(rc, 3, err)
        self.assertEqual(sorted(os.listdir(self.work)), before, "a refused process leaves no trace in the root")


FAKE_CLAUDE = ("#!%s\nimport sys\nsys.stdin.read()\nopen(sys.argv[0] + '.calls', 'a').write('call\\n')\n"
               "print('{\"ok\": true, \"url\": \"u\"}')\n")


class FreshDirTest(Base):
    def test_each_leftover_of_an_older_layout_is_refused_naming_it(self):
        for rel in LEGACY + ("state/done",):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as d:
                p = Path(d) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.mkdir() if rel.endswith("sending") else p.write_text("x")
                rc, err = self.main(d)
                self.assertEqual(rc, 3 if rel == "state/connector.pid" else 2, err)
                self.assertIn(rel, err)
                self.assertFalse((Path(d) / "state" / "format").exists())

    def test_an_empty_dir_starts_and_marks_itself_then_its_own_state_restarts(self):
        self.assertEqual(self.main()[0], 0)
        self.assertEqual((self.work / "state" / "format").read_text(), "cloud-session/1\n")
        self.task()
        rc, err = self.main()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(self.work / "state")), ["claims", "format", "rate.jsonl", "settled-ids"])
        self.assertIn("task-a", (self.work / "state" / "settled-ids").read_text())
        self.assertEqual(self.main()[0], 0, "this binary's own state restarts")

    def test_with_the_marker_only_the_allowlist_is_accepted(self):
        self.assertEqual(self.main()[0], 0)
        for name in ("done", ".rate.jsonl.123.abcd.tmp", ".rate.jsonl.456.abcd.tmp"):
            (self.work / "state" / name).write_text("")
        old = time.time() - vendored.TEMP_SWEEP_AFTER - 5
        os.utime(self.work / "state" / ".rate.jsonl.123.abcd.tmp", (old, old))
        self.assertEqual(self.main()[0], 0)
        self.assertFalse((self.work / "state" / ".rate.jsonl.123.abcd.tmp").exists(), "a crash's temp is cleared")
        self.assertTrue((self.work / "state" / ".rate.jsonl.456.abcd.tmp").exists(),
                        "a fresh temp may be a live writer's, mid-replace: kept, and the start still accepted")
        stale = self.work / "state" / "connector.pid"
        stale.write_text("abc")
        os.utime(stale, (time.time() - 100, time.time() - 100))
        rc, err = self.main()
        self.assertEqual(rc, 2, "a stale pidfile that is not one dead pid is refused: %s" % err)
        self.assertIn("connector.pid", err)
        stale.unlink()
        (self.work / "state" / ".format.123.abcdef.tmp").write_text("cloud-session/1\n")
        rc, err = self.main()
        self.assertEqual(rc, 2, "a marker temp beside the marker is refused")
        self.assertIn(".format.123.abcdef.tmp", err)
        (self.work / "state" / ".format.123.abcdef.tmp").unlink()
        for name in ("sends.json", "sending", "fires.json"):
            with self.subTest(name=name):
                p = self.work / "state" / name
                p.mkdir() if name == "sending" else p.write_text("")
                rc, err = self.main()
                self.assertEqual(rc, 2)
                self.assertIn("state/" + name, err)
                (os.rmdir if p.is_dir() else os.unlink)(p)

    def test_an_attributed_dead_legacy_pidfile_is_removed_once_and_anything_else_refuses(self):
        state = self.work / "state"
        state.mkdir()
        writer = subprocess.Popen([sys.executable, "-c", PRIOR_WRITER % str(state)], stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(lambda: (writer.kill(), writer.wait(10)))
        _, pid = read_line(writer).split()
        writer.send_signal(signal.SIGTERM)
        writer.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and task_io.pid_alive(int(pid)):
            time.sleep(0.02)
        pidfile = state / "connector.pid"
        self.assertEqual(pidfile.read_text(), "%s\n" % pid, "SIGTERMed, its finally never ran")
        time.sleep(1.1)
        self.task()
        rc, err = self.main_in_a_new_process()
        self.assertEqual(rc, 0, err)
        self.assertFalse(pidfile.exists(), "removed once")
        self.assertTrue((self.results / "task-a.txt").exists(), "and the adapter served")
        for name, text, mtime_ahead in (("written after this start", "%s\n" % pid, 5), ("two records", "12\n34\n", -100),
                                         ("a stray word", "pid\n", -100)):
            with self.subTest(case=name):
                pidfile.write_text(text)
                when = time.time() + mtime_ahead
                os.utime(pidfile, (when, when))
                rc, err = self.main_in_a_new_process()
                self.assertEqual(rc, 2, err)
                self.assertIn(str(pidfile), err)
                self.assertTrue(pidfile.exists(), "never removed when it is not attributed")
        live = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (live.kill(), live.wait(10)))
        pidfile.write_text("%d\n" % live.pid)
        rc, err = self.main_in_a_new_process()
        self.assertEqual(rc, 3, "a live pid is the parent's LegacyRunning: %s" % err)
        self.assertIn("stop the old adapter (pid %d)" % live.pid, err)

    def test_first_init_order_and_a_crash_at_every_boundary(self):
        steps = []
        with mock.patch.object(self.lc, "_init_step", steps.append):
            self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.assertEqual(steps, ["dirs", "ledger", "claims", "format-temp", "format", "done"])
        for boundary in steps:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as d:
                def crash(name, at=boundary):
                    if name == at:
                        raise Crash()
                with mock.patch.object(self.lc, "_init_step", crash), self.assertRaises(Crash):
                    self.lc.prepare_work_dir(d)
                self.assertEqual(os.path.exists(Path(d) / "state" / "format"), boundary in ("format", "done"))
                self.assertIsNone(self.lc.prepare_work_dir(d), "an interrupted init completes")
                self.assertEqual((Path(d) / "state" / "settled-ids").read_bytes(), b"")
                self.assertTrue((Path(d) / "state" / "claims").is_dir())
                self.assertIsNone(self.lc.prepare_work_dir(d))

    def test_the_init_prefixes_by_lstat_and_fsync(self):
        def table(d):
            out = {}
            for name in sorted(os.listdir(Path(d) / "state")):
                st = os.lstat(Path(d) / "state" / name)
                kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
                key = ".format.<tmp>" if re.fullmatch(r"\.format\.\d+\.[0-9a-f]{6}\.tmp", name) else name
                out[key] = (kind, st.st_size if kind == "file" else len(os.listdir(Path(d) / "state" / name)))
            return out
        expected = {
            "dirs": ({}, []),
            "ledger": ({"settled-ids": ("file", 0)}, ["fsync settled-ids", "dir state"]),
            "claims": ({"settled-ids": ("file", 0), "claims": ("dir", 0)}, ["dir claims", "dir state"]),
            "format-temp": ({"settled-ids": ("file", 0), "claims": ("dir", 0), ".format.<tmp>": ("file", 16)},
                            ["fsync <temp>"]),
            "format": ({"settled-ids": ("file", 0), "claims": ("dir", 0), "format": ("file", 16)},
                       ["replace format", "dir state"]),
            "done": ({"settled-ids": ("file", 0), "claims": ("dir", 0), "format": ("file", 16)},
                     ["dir state", "dir WORK", "dir PARENT"]),
        }
        for boundary, (want, step_fsyncs) in expected.items():
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as parent:
                d = os.path.join(parent, "work")
                os.mkdir(d)
                events = []
                names = {os.path.basename(d): "WORK", os.path.basename(parent): "PARENT"}

                def crash(name, at=boundary):
                    events.append("|" + name)
                    if name == at:
                        raise Crash()
                with contextlib.ExitStack() as stack:
                    for patch in durable_events(self.lc, events):
                        stack.enter_context(patch)
                    stack.enter_context(mock.patch.object(self.lc, "_init_step", crash))
                    with self.assertRaises(Crash):
                        self.lc.prepare_work_dir(d)
                events = [" ".join([e.split(" ")[0], names.get(e.split(" ", 1)[1], e.split(" ", 1)[1])])
                          if " " in e else e for e in events]
                segments, current = {}, []
                for e in events:
                    if e.startswith("|"):
                        segments[e[1:]], current = current, []
                    else:
                        current.append(e)
                self.assertEqual(table(d), want, "the on-disk prefix, by lstat")
                self.assertEqual(segments[boundary], step_fsyncs, "the fsyncs of this step")
                self.assertIsNone(self.lc.prepare_work_dir(d), "the interrupted init completes")
                self.assertIsNone(self.lc.prepare_work_dir(d))

    def test_a_marker_interrupted_between_its_temp_fsync_and_rename_completes_once(self):
        def crash(name):
            if name == "format-temp":
                raise Crash()
        with mock.patch.object(self.lc, "_init_step", crash), self.assertRaises(Crash):
            self.lc.prepare_work_dir(str(self.work))
        temps = [n for n in os.listdir(self.work / "state") if n.startswith(".format.")]
        self.assertEqual(len(temps), 1)
        self.assertFalse((self.work / "state" / "format").exists())
        removed = []
        real = os.unlink
        with mock.patch.object(self.lc.os, "unlink", lambda p, *a, **k: (removed.append(os.path.basename(p)),
                                                                           real(p, *a, **k))[1]):
            self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
            self.assertEqual(removed, temps, "the interrupted temp is cleared exactly once")
            self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.assertEqual(removed, temps, "and a marked dir clears nothing more")
        self.assertEqual((self.work / "state" / "format").read_text(), "cloud-session/1\n")
        self.assertEqual([n for n in os.listdir(self.work / "state") if n.startswith(".format.")], [])

    def test_a_format_temp_that_is_not_one_regular_file_refuses(self):
        def fresh():
            d = Path(tempfile.mkdtemp(dir=self.tmp.name))
            (d / "state" / "claims").mkdir(parents=True)
            (d / "state" / "settled-ids").write_text("")
            return d
        cases = {
            "two temps": lambda d: [(d / "state" / (".format.%d.abcdef.tmp" % i)).write_text("x") for i in (1, 2)],
            "a directory": lambda d: (d / "state" / ".format.1.abcdef.tmp").mkdir(),
            "a symlink": lambda d: os.symlink(d / "state" / "settled-ids", d / "state" / ".format.1.abcdef.tmp"),
            "not the temp's name": lambda d: (d / "state" / ".format.1.abcdef12.tmp").write_text("x"),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                d = fresh()
                make(d)
                line = self.lc.prepare_work_dir(str(d))
                self.assertIsNotNone(line)
                self.assertIn(".format.", line)
                self.assertFalse((d / "state" / "format").exists())

    def test_wrong_types_and_symlinks_in_state_refuse(self):
        def fresh():
            d = Path(tempfile.mkdtemp(dir=self.tmp.name))
            (d / "state").mkdir()
            return d
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        (other / "empty").write_text("")
        (other / "dir").mkdir()
        cases = {
            "ledger is a directory": lambda d: (d / "state" / "settled-ids").mkdir(),
            "claims is a file": lambda d: (d / "state" / "claims").write_text(""),
            "ledger is a symlink": lambda d: os.symlink(other / "empty", d / "state" / "settled-ids"),
            "claims is a symlink": lambda d: os.symlink(other / "dir", d / "state" / "claims"),
            "state is a symlink": lambda d: (os.rmdir(d / "state"), os.symlink(other / "dir", d / "state")),
        }
        for what, make in cases.items():
            with self.subTest(what=what):
                d = fresh()
                make(d)
                line = self.lc.prepare_work_dir(str(d))
                self.assertIsNotNone(line, what)
                self.assertIn("state", line)
                self.assertFalse(os.path.lexists(d / "state" / "format") and not os.path.islink(d / "state"))
        for what, swap in (("ledger", "settled-ids"), ("claims", "claims")):
            with self.subTest(marked=what):
                d = fresh()
                os.rmdir(d / "state")
                self.assertIsNone(self.lc.prepare_work_dir(str(d)))
                target = d / "state" / swap
                if target.is_dir():
                    os.rmdir(target)
                    target.write_text("")
                else:
                    target.unlink()
                    os.symlink(other / "empty", target)
                self.assertIn(swap, self.lc.prepare_work_dir(str(d)))

    def test_an_interrupted_init_with_anything_else_refuses(self):
        (self.work / "state").mkdir()
        (self.work / "state" / "settled-ids").write_text("")
        (self.work / "state" / "claims").mkdir()
        (self.work / "state" / "claims" / "task-a").mkdir()
        self.assertIn("state/claims/task-a", self.lc.prepare_work_dir(str(self.work)))
        os.rmdir(self.work / "state" / "claims" / "task-a")
        (self.work / "state" / "settled-ids").write_text("task-a\n")
        self.assertIn("state/settled-ids", self.lc.prepare_work_dir(str(self.work)))

    def test_a_marker_without_its_ledger_or_claims_refuses(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        for name in ("settled-ids", "claims"):
            with self.subTest(name=name):
                p = self.work / "state" / name
                moved = self.work / ("moved-" + name)
                os.rename(p, moved)
                self.assertIn("%s is missing" % name, self.lc.prepare_work_dir(str(self.work)))
                os.rename(moved, p)

    def test_an_optional_limiter_record_must_be_a_plain_file(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "absent: accepted")
        p = self.work / "state" / "collaborator-turns.json"
        p.write_text("[]")
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "a plain file: accepted")
        p.unlink()
        p.mkdir()
        line = self.lc.prepare_work_dir(str(self.work))
        self.assertIn("state/collaborator-turns.json in", line)
        self.assertIn("is not a plain file", line)
        p.rmdir()
        os.symlink(self.work / "state" / "settled-ids", p)
        self.assertIn("is not a plain file", self.lc.prepare_work_dir(str(self.work)), "a link is not a plain file")
        os.unlink(p)
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.assertIn("collaborator-turns.json", self.lc.STATE_OPTIONAL_FILES)

    def test_the_bridge_may_create_tasks_and_results_first(self):
        (self.work / "tasks").mkdir()
        (self.work / "tasks" / "task-early.txt").write_text("id: task-early\n")
        (self.work / "results").mkdir()
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "gateway-state").mkdir()
            self.assertIn("gateway-state", self.lc.prepare_work_dir(d))

    def test_the_startup_order(self):
        calls = []
        real_acquire, real_publish = holder_lock.acquire, holder_lock.publish
        real = {n: getattr(self.lc, n) for n in ("prepare_work_dir", "probe_moves")}
        real_gateway = sa.gateway_state_conflict

        def dirs():
            return all((self.work / d).is_dir() for d in ("tasks", "results", "state"))

        def wrap(name, fn):
            return lambda *a, **k: (calls.append((name, dirs())), fn(*a, **k))[1]
        root = self.work / "fresh-root"
        self.work = root
        with mock.patch.object(holder_lock, "acquire", wrap("lock", real_acquire)), \
                mock.patch.object(holder_lock, "publish", wrap("publish", real_publish)), \
                mock.patch.object(self.lc, "prepare_work_dir", wrap("fresh-dir", real["prepare_work_dir"])), \
                mock.patch.object(self.lc, "probe_moves", wrap("probe", real["probe_moves"])), \
                mock.patch.object(sa, "gateway_state_conflict", wrap("gateway-state", real_gateway)):
            self.assertEqual(self.main()[0], 0)
        self.assertEqual(calls, [("lock", False), ("publish", False), ("gateway-state", True), ("fresh-dir", True),
                                 ("probe", True), ("probe", True)],
                         "lock, publish, then the directories, then the checks in order")

    def test_a_foreign_marker_and_enumeration_errors_fail_closed(self):
        self.assertEqual(self.main()[0], 0)
        (self.work / "state" / "format").write_text("cloud-session/0\n")
        rc, err = self.main()
        self.assertEqual(rc, 2)
        self.assertIn("format", err)
        (self.work / "state" / "format").write_text("cloud-session/1\n")
        self.deny(self.work / "state")
        self.assertIn("cannot check", self.lc.prepare_work_dir(str(self.work)))

    def test_the_topology_round_trip_with_a_bridge_stand_in(self):
        base = Path(self.tmp.name + "-topo")
        base.mkdir()
        self.addCleanup(shutil.rmtree, base, True)
        cloud, gateway = base / "cloud-session", base / "gateway-state"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(base)}
        flag = ["--gateway-state", str(gateway)]
        self.assertEqual(self.main(cloud, env, flag)[0], 0, "clean init")
        self.work = cloud
        self.task("task-rt")
        self.assertEqual(self.main(cloud, env, flag)[0], 0, "answered")
        live = cloud / "results" / "task-rt.txt"
        _, gen = result_ready.identity_of(live)
        gateway.mkdir()
        (gateway / "gateway-status.json").write_text("{}")
        done = result_disposal.retire_generation(cloud / "results", live, gen, quiet, cloud / "results" / "archive",
                                                 ["task-rt-1.txt"])
        self.assertTrue(done.retired, "the bridge stand-in claimed the result")
        rc, err = self.main(cloud, env, flag)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.lc.ClaimStore(cloud / "state" / "claims").load("task-rt")["phase"], "SETTLED")
        self.assertEqual(sorted(os.listdir(cloud / "results" / "archive")), ["task-rt-1.txt"], "nothing added")
        for bad in (str(cloud / "gw"), str(cloud), "relative/gw"):
            with self.subTest(gateway_state=bad):
                rc, err = self.main(cloud, env, ["--gateway-state", bad])
                self.assertEqual(rc, 2)
                self.assertIn(bad, err)

    def test_a_claim_io_error_exits_2_naming_the_path_and_keeps_the_claim(self):
        self.assertEqual(self.main()[0], 0)
        self.task()
        real = self.lc.write_durable

        def fail_claim(path, data):
            if str(path).endswith("claim.json") and b'"PUBLISHED"' in data:
                raise OSError(28, "No space left on device")
            return real(path, data)
        with mock.patch.object(self.lc, "write_durable", fail_claim):
            rc, err = self.main()
        self.assertEqual(rc, 2)
        self.assertIn("claim.json", err)
        rec = self.lc.ClaimStore(self.work / "state" / "claims").load("task-a")
        self.assertEqual(rec["phase"], "RESULT_STAGED", "the claim is kept as it was")
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.lc.ClaimStore(self.work / "state" / "claims").load("task-a")["phase"], "SETTLED")

    def test_an_unreadable_claim_is_never_called_and_is_named(self):
        self.assertEqual(self.main()[0], 0)
        self.task()
        claim = self.work / "state" / "claims" / "task-a" / "claim.json"
        claim.parent.mkdir()
        claim.write_text("{torn")
        code = ("import sys\nsys.path.insert(0, %r)\n"
                "from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa\n"
                "sys.exit(sa.main(['--work', %r, '--session-id', 'session_X', '--claude', %r, '--poll', '0.05'],"
                " {'PATH': '/usr/bin:/bin', 'HOME': %r}))\n"
                % (str(PKG_ROOT), str(self.work), self.tmp.name + "-bin/claude", self.tmp.name))
        child = subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True)
        err, deadline = "", time.monotonic() + 10
        try:
            while str(claim) not in err and time.monotonic() < deadline and child.poll() is None:
                if select.select([child.stderr], [], [], 0.2)[0]:
                    err += child.stderr.readline()
        finally:
            child.kill()
            child.wait(10)
        self.assertIn(str(claim), err, "the parked claim is named")
        self.assertEqual(self.calls(), 0, "no provider call")
        self.assertEqual(claim.read_text(), "{torn", "the claim is kept")
        self.assertFalse((self.work / "results" / "task-a.txt").exists())
        self.assertEqual((self.work / "state" / "settled-ids").read_text(), "")

    def test_the_modules_read_no_legacy_file(self):
        for path in (REPO_SRC / "task_lifecycle.py", PKG_ROOT / "ag2_sparrow" / "task_lifecycle.py",
                     ADAPTERS / "claude_cloud_session" / "session_adapter.py"):
            if not path.exists():
                continue
            strings = [n.value for n in ast.walk(ast.parse(path.read_text()))
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            for word in ("sends.json", "fires.json", "sending", ".staged"):
                self.assertFalse([v for v in strings if word in v], (path.name, word))

    def test_a_failed_append_stops_the_adapter_and_the_next_start_repairs(self):
        self.assertEqual(self.main()[0], 0)
        self.task()
        with mock.patch.object(self.lc.SettledIds, "add", side_effect=self.lc.LedgerError("cannot append (EROFS)")):
            rc, err = self.main()
        self.assertEqual(rc, 2)
        self.assertIn("cannot append", err)
        rec = self.lc.ClaimStore(self.work / "state" / "claims").load("task-a")
        self.assertEqual(rec["phase"], "SETTLED")
        ledger = self.work / "state" / "settled-ids"
        self.assertNotIn("task-a", ledger.read_text() if ledger.exists() else "")
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(ledger.read_text(), "task-a\n")


class ProbeTest(Base):
    def test_rename_and_link_probe(self):
        self.assertIsNone(self.lc.probe_moves(str(self.work)))
        with mock.patch.object(self.lc.os, "link", side_effect=OSError(1, "no links")):
            self.assertIn("hard link", self.lc.probe_moves(str(self.work)))
        self.assertEqual(os.listdir(self.work), [])

    def test_a_filesystem_without_the_no_replace_move_fails_the_probe(self):
        failing = FileExistsError(errno.ENOTSUP, "no no-replace rename")
        with mock.patch.object(self.lc.undelivered_quarantine, "move_private", side_effect=failing):
            self.assertIn("no-replace rename", self.lc.probe_moves(str(self.work)))
        self.assertEqual(os.listdir(self.work), [], "the probe files are removed")


# ── the classifier ───────────────────────────────────────────────────────────

class ProbeBothDirsTest(Base):
    def test_the_rename_and_link_probes_run_in_results_and_in_state(self):
        for op in ("rename", "link"):
            for d in ("results", "state"):
                with self.subTest(op=op, dir=d):
                    def failing(real, where=d):
                        def fn(a, b, **k):
                            if os.path.basename(os.path.dirname(os.fspath(a))) == where and ".probe-" in os.fspath(a):
                                raise OSError(18, "Cross-device link", str(a))
                            return real(a, b, **k)
                        return fn
                    names = ("rename", "replace") if op == "rename" else ("link",)
                    self.setUp()
                    with contextlib.ExitStack() as stack:
                        for n in names:
                            stack.enter_context(mock.patch.object(os, n, failing(getattr(os, n))))
                        rc, err = FreshDirTest.main(self)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(os.path.join(str(self.work), d), err)


class ClassifyTest(Base):
    def setUp(self):
        super().setUp()
        self.results.mkdir()

    def put(self, rel, text="body\n"):
        p = self.results / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def claim_name(self, tid="task-a"):
        return ".%s.disposing-%d-1-1-1-1-%s-abcd1234" % (tid, os.getpid(), "0" * 64)

    def state(self, tid="task-a"):
        return self.lc.classify(self.results, tid)

    def test_each_state(self):
        self.assertEqual(self.state(), "ABSENT")
        self.put("task-a.txt", "  \n")
        self.assertEqual(self.state(), "LIVE_UNREADY")
        self.put("task-a.txt")
        self.assertEqual(self.state(), "LIVE_READY")
        self.assertEqual(self.lc.classify(self.work / "nowhere", "task-a"), "UNKNOWN")

    def test_claimed_is_the_disposal_claim_not_a_sending_name(self):
        self.put("task-a.sending.123")
        self.assertEqual(self.state(), "ABSENT")
        self.put(self.claim_name())
        self.assertIsNotNone(result_disposal.parse_claim(self.results / self.claim_name()))
        self.assertEqual(self.state(), "CLAIMED")

    def test_archived_in_every_layout(self):
        for rel in ("archive/task-a.txt", "archive/2026-10/task-a.txt", "archive-2026-10-01/task-a.txt"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as d:
                (Path(d) / rel).parent.mkdir(parents=True)
                (Path(d) / rel).write_text("x\n")
                self.assertEqual(self.lc.classify(d, "task-a"), "ARCHIVED")

    def test_quarantined_is_hash_free(self):
        self.put("undelivered/task-a-123.txt", "some other body\n")
        self.assertEqual(self.state(), "QUARANTINED")

    def test_precedence_pairs(self):
        pairs = [
            ({"task-a.txt": "x\n", "archive/task-a.txt": "y\n"}, "LIVE_READY"),
            ({"archive/task-a.txt": "y\n", "undelivered/task-a-1.txt": "z\n"}, "ARCHIVED"),
            ({"undelivered/task-a-1.txt": "z\n", "CLAIM": "c"}, "QUARANTINED"),
            ({"CLAIM": "c", "task-a.txt": ""}, "CLAIMED"),
            ({"task-a.txt": "", "archive/task-a.txt": "y\n"}, "ARCHIVED"),
            ({"task-a.txt": ""}, "LIVE_UNREADY"),
        ]
        for files, want in pairs:
            with self.subTest(files=sorted(files)), tempfile.TemporaryDirectory() as d:
                for rel, text in files.items():
                    rel = self.claim_name() if rel == "CLAIM" else rel
                    (Path(d) / rel).parent.mkdir(parents=True, exist_ok=True)
                    (Path(d) / rel).write_text(text)
                self.assertEqual(self.lc.classify(d, "task-a"), want)

    def test_any_lookup_error_is_unknown_and_beats_everything(self):
        self.put("task-a.txt")
        self.put("archive/2026-10/other.txt")
        self.deny(self.results / "archive" / "2026-10")
        self.assertEqual(self.state(), "UNKNOWN")

    def test_a_quarantine_lookup_error_is_unknown(self):
        self.put("undelivered/other-1.txt")
        self.deny(self.results / "undelivered")
        self.assertEqual(self.state(), "UNKNOWN")

    def test_an_unreadable_live_or_archive_is_unknown(self):
        self.put("archive/task-a.txt", "")
        self.assertEqual(self.state(), "UNKNOWN", "an empty archive parks")
        os.unlink(self.results / "archive" / "task-a.txt")
        self.put("task-a.txt")
        self.deny(self.results / "task-a.txt")
        self.assertEqual(self.state(), "UNKNOWN")


# ── publication ──────────────────────────────────────────────────────────────

class Crash(BaseException):
    pass


class PublicationTest(Base):
    def test_publish_then_settle_writes_once_and_records_won(self):
        life = self.life()
        self.run_to_outcome(life)
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["published"]["won"]), ("SETTLED", True))
        self.assertFalse((life.claims.dir("task-a") / "body").exists(), "the stage goes at SETTLED")
        self.assertEqual([n for n in os.listdir(self.results) if n.startswith(".task-")], [])
        self.assertIn("task-a", life.settled)

    def test_the_durable_write_order(self):
        life = self.life()
        self.run_to_outcome(life)
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            life.publish_and_settle("task-a")
        publish = events[:events.index("final task-a.txt") + 2]
        self.assertEqual(publish, ["fsync <temp>", "replace .task-a.txt.publish.1", "dir results",
                                   "final task-a.txt", "dir results"],
                         "temp fsynced, sibling, dir, no-replace final, dir")
        rest = events[len(publish):]
        claim = ["fsync <temp>", "replace claim.json", "dir task-a"]
        self.assertEqual(rest[:3], claim, "PUBLISHED: file, rename, dir")
        settled = rest[3:]
        self.assertEqual(settled[:3], claim, "SETTLED before the ledger")
        self.assertEqual(settled[3:5], ["fsync settled-ids", "dir state"],
                         "the ledger itself fsynced, then state/ on creation")

    def test_the_durable_write_order_from_prepared_to_the_ledger(self):
        life = self.life()
        path = self.task()
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            n = life.prepare("task-a", "i\n")
            life.begin_call("task-a", n, path)
            life.record_outcome("task-a", "queued", "[no-send]\n")
            life.publish_and_settle("task-a")
        claim = ["fsync <temp>", "replace claim.json", "dir task-a"]
        prepared_and_reserved = ["dir claims"] + claim + ["fsync rate.jsonl", "dir state", "fsync rate.jsonl"]
        stage = ["fsync <temp>", "replace body", "dir task-a"]
        confirmed_and_queued = claim + ["fsync rate.jsonl"]
        publication = ["fsync <temp>", "replace .task-a.txt.publish.1", "dir results", "final task-a.txt",
                       "dir results"]
        ledger_created = ["fsync settled-ids", "dir state"]
        self.assertEqual(events, prepared_and_reserved + claim + stage + confirmed_and_queued + claim
                         + publication + claim + claim + ledger_created,
                         "PREPARED, CALLING, stage, CONFIRMED, RESULT_STAGED, publication, PUBLISHED, SETTLED, ledger")

    def sequence(self, fsync_dir=None):
        """Every durable event of one task, PREPARED to the ledger, in a fresh work dir."""
        self.work = Path(tempfile.mkdtemp(dir=self.tmp.name))
        life = self.life()
        path = self.task()
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            if fsync_dir is not None:
                stack.enter_context(mock.patch.object(self.lc, "fsync_dir", fsync_dir))
            n = life.prepare("task-a", "i\n")
            life.begin_call("task-a", n, path)
            life.record_outcome("task-a", "queued", "[no-send]\n")
            life.publish_and_settle("task-a")
        return events

    def test_a_no_op_or_wrong_descriptor_directory_fsync_is_detected(self):
        good = self.sequence()
        self.assertIn("dir results", good)
        real = self.lc.fsync_dir
        noop = self.sequence(lambda p: None)
        self.assertNotEqual(noop, good, "a directory fsync that does nothing is seen")
        self.assertNotIn("dir results", noop)
        wrong = self.sequence(lambda p: real(os.path.dirname(os.path.abspath(p))))
        self.assertNotEqual(wrong, good, "an fsync of the wrong directory, in place of the right one, is seen")
        self.assertEqual(len(wrong), len(good), "it replaced the correct fsync one for one")

    def test_a_temp_altered_before_the_read_back_is_never_published(self):
        for how in ("one byte", "a substituted file"):
            with self.subTest(how=how):
                self.work = Path(tempfile.mkdtemp(dir=self.tmp.name))
                life = self.life()
                self.run_to_outcome(life)
                real_fsync = os.fsync

                def alter(fd, how=how):
                    real_fsync(fd)
                    path = fd_path(fd)
                    name = os.path.basename(path)
                    if name.startswith(".task-a.publish.") and name.endswith(".tmp"):
                        if how == "one byte":
                            with open(path, "r+b") as f:
                                first = f.read(1)
                                f.seek(0)
                                f.write(bytes([first[0] ^ 1]))
                        else:
                            with open(path + ".other", "wb") as f:
                                f.write(b"[no-send]\n"[::-1])
                            os.replace(path + ".other", path)
                with mock.patch.object(self.lc.os, "fsync", alter):
                    self.assertEqual(life.publish_and_settle("task-a"), "parked")
                self.assertFalse((self.results / "task-a.txt").exists(), "nothing visible")
                self.assertEqual([n for n in os.listdir(self.results) if n.startswith(".task-a")], [])
                self.assertEqual(life.publish_and_settle("task-a"), "settled")
                self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n", "published once, whole")

    def test_an_existing_live_result_is_never_replaced(self):
        life = self.life()
        self.run_to_outcome(life)
        (self.results / "task-a.txt").write_text("theirs\n")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_text(), "theirs\n")
        self.assertFalse(life.claims.load("task-a")["published"]["won"])

    def test_eexist_on_a_ready_result_is_lost_and_on_an_unready_one_parks(self):
        life = self.life()
        self.run_to_outcome(life)
        real = self.lc.undelivered_quarantine.move_private

        def someone_first(text):
            def move(src, dst, log=None):
                Path(dst).write_text(text)
                return real(src, dst, log)
            return move
        with mock.patch.object(self.lc.undelivered_quarantine, "move_private", someone_first("")):
            self.assertEqual(life.publish_and_settle("task-a"), "parked")
        self.assertEqual((self.results / "task-a.txt").read_text(), "", "an unready result is left alone")
        self.assertTrue((self.results / ".task-a.txt.publish.1").exists(), "the sibling waits")
        os.unlink(self.results / "task-a.txt")
        with mock.patch.object(self.lc.undelivered_quarantine, "move_private", someone_first("theirs\n")):
            self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_text(), "theirs\n")
        self.assertFalse((self.results / ".task-a.txt.publish.1").exists())
        self.assertFalse(life.claims.load("task-a")["published"]["won"])

    def crash_at(self, target, attr):
        life = self.life()
        self.run_to_outcome(life)
        real = getattr(target, attr)
        hits = []

        def boom(*a, **k):
            out = real(*a, **k)
            hits.append(1)
            raise Crash()
        with mock.patch.object(target, attr, boom), self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertTrue(hits)
        again = self.life()
        again.recover()
        return again

    def test_crash_after_the_sibling_before_the_rename_republishes_once(self):
        again = self.crash_at(self.lc, "fsync_dir")
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")
        self.assertEqual([n for n in os.listdir(self.results) if n.startswith(".task-")], [])

    def test_crash_after_the_rename_before_the_record_settles_as_won(self):
        again = self.crash_at(self.lc.undelivered_quarantine, "move_private")
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")
        rec = again.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["published"]["won"]), ("SETTLED", True))

    def test_crash_after_published_before_settled(self):
        life = self.life()
        self.run_to_outcome(life)
        real = self.lc.ClaimStore.save

        def crash_at_settled(store, rec):
            if rec["phase"] == "SETTLED":
                raise Crash()
            return real(store, rec)
        with mock.patch.object(self.lc.ClaimStore, "save", crash_at_settled), self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertEqual(life.claims.load("task-a")["phase"], "PUBLISHED")
        again = self.life()
        again.recover()
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")
        self.assertIn("task-a", again.settled)
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")

    def test_a_crash_before_the_sibling_rename_leaves_nothing_visible(self):
        life = self.life()
        self.run_to_outcome(life)
        with mock.patch.object(self.lc.os, "replace", side_effect=Crash), \
                mock.patch.object(self.lc.os, "rename", side_effect=Crash), self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertFalse((self.results / "task-a.txt").exists())
        self.life().recover()
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")

    def test_a_crash_during_the_temp_write_leaves_nothing_visible(self):
        """The process is killed mid-write (SIGXFSZ at a 4 KiB file-size limit), leaving a real partial temp."""
        body = "x" * 20000 + "\n"
        life = self.life()
        self.run_to_outcome(life, body=body)
        load = ("from ag2_sparrow import task_lifecycle as lc" if self.lc is vendored else
                "import importlib.util\nspec = importlib.util.spec_from_file_location('lc', %r)\n"
                "lc = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(lc)"
                % str(REPO_SRC / "task_lifecycle.py"))
        code = ("import resource, signal, sys\nsys.path.insert(0, %r)\n%s\nlife = lc.Lifecycle(%r, log=lambda *a: None)\n"
                "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
                "resource.setrlimit(resource.RLIMIT_FSIZE, (4096, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))\n"
                "life.publish_and_settle('task-a')\nprint('survived')\n" % (str(PKG_ROOT), load, str(self.work)))
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, -signal.SIGXFSZ, child.stdout + child.stderr)
        partial = [p for p in self.results.iterdir() if p.name.startswith(".task-a") and p.stat().st_size < len(body)]
        self.assertTrue(partial, "an incomplete temp was on disk when the process died")
        self.assertFalse((self.results / "task-a.txt").exists())
        self.assertEqual(self.lc.classify(self.results, "task-a"), "ABSENT", "nothing visible as a result")
        again = self.life()
        again.recover()
        self.assertEqual((self.results / "task-a.txt").read_text(), body, "published whole, once")
        self.assertEqual([n for n in os.listdir(self.results) if n.startswith(".task-")], [])
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")

    def test_publish_then_immediate_quarantine_then_crash_writes_nothing_more(self):
        life = self.life()
        self.run_to_outcome(life)

        def quarantined_then_crash(final):
            self.lc.result_disposal.quarantine_current(self.results, Path(final), quiet)
            raise Crash()
        life.read_final = quarantined_then_crash
        with self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertEqual(self.lc.classify(self.results, "task-a"), "QUARANTINED")
        self.life().recover()
        self.assertFalse((self.results / "task-a.txt").exists(), "nothing republished")
        rec = self.life().claims.load("task-a")
        self.assertEqual((rec["phase"], rec["published"]["state"], rec["published"]["won"]),
                         ("SETTLED", "QUARANTINED", True))

    def test_a_final_file_taken_after_the_rename_still_reads_as_won(self):
        life = self.life()
        self.run_to_outcome(life)
        (self.results / "archive").mkdir()
        real = life.read_final

        def retired_first(final):
            _, gen = self.lc.result_ready.identity_of(final)
            self.lc.result_disposal.retire_generation(self.results, Path(final), gen, quiet,
                                                      self.results / "archive", ["task-a-1.txt"])
            return real(final)
        life.read_final = retired_first
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["published"]["won"], rec["published"]["state"]), (True, "ARCHIVED"))
        self.assertFalse((self.results / "task-a.txt").exists(), "no retry after a won rename")

    def test_a_final_file_taken_into_a_disposal_claim_after_the_rename_still_reads_as_won(self):
        life = self.life()
        self.run_to_outcome(life)
        real = life.read_final
        claim = self.results / (".task-a.disposing-%d-1-1-1-1-%s-abcd1234" % (os.getpid(), "0" * 64))

        def claimed_first(final):
            os.rename(final, claim)
            return real(final)
        life.read_final = claimed_first
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["published"]["won"], rec["published"]["state"]),
                         ("SETTLED", True, "CLAIMED"))
        again = self.life()
        again.recover()
        self.assertIn("task-a", again.settled)
        self.assertFalse((self.results / "task-a.txt").exists(), "never republished")
        self.assertEqual(claim.read_text(), "[no-send]\n")

    def test_a_quarantined_copy_with_other_bytes_is_lost(self):
        life = self.life()
        self.run_to_outcome(life)
        (self.results / "undelivered").mkdir()
        (self.results / "undelivered" / "task-a-1.txt").write_text("someone else's answer\n")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["published"]["won"], rec["published"]["state"]), (False, "QUARANTINED"))
        self.assertFalse((self.results / "task-a.txt").exists())

    def test_the_final_check_runs_under_the_disposal_lock(self):
        life = self.life()
        self.run_to_outcome(life)
        busy = []
        real = life.read_final

        def other_thread_tries():
            try:
                with result_disposal.locked(self.results):
                    busy.append(False)
            except result_disposal.DisposalBusy:
                busy.append(True)

        def check(final):
            t = threading.Thread(target=other_thread_tries)
            t.start()
            t.join(10)
            return real(final)
        life.read_final = check
        with mock.patch.object(result_disposal, "LOCK_WAIT_S", 0.2):
            life.publish_and_settle("task-a")
        self.assertEqual(busy, [True], "another locker is held off while the final file is read")

    def test_parked_states_never_publish(self):
        for setup in ("claim", "unready"):
            with self.subTest(setup=setup):
                self.setUp()
                life = self.life()
                self.run_to_outcome(life)
                if setup == "claim":
                    (self.results / (".task-a.disposing-%d-1-1-1-1-%s-abcd1234" % (os.getpid(), "0" * 64))).write_text("")
                else:
                    (self.results / "task-a.txt").write_text("")
                self.assertEqual(life.publish_and_settle("task-a"), "parked")
                self.assertEqual(life.claims.load("task-a")["phase"], "RESULT_STAGED")

    def test_a_busy_disposal_lock_parks(self):
        life = self.life()
        self.run_to_outcome(life)
        with mock.patch.object(self.lc.result_disposal, "locked", side_effect=result_disposal.DisposalBusy("busy")):
            self.assertEqual(life.publish_and_settle("task-a"), "parked")
            self.assertEqual(life.decide("task-a"), ("park", "UNKNOWN"))


# ── the journal ──────────────────────────────────────────────────────────────

class JournalTest(Base):
    def journal(self):
        (self.work / "state").mkdir(exist_ok=True)
        return self.lc.RateJournal(self.work / "state" / "rate.jsonl", self.clock)

    def test_reducer_precedence(self):
        j = self.journal()
        j.append("t1", 1, "reserved", 100)
        j.append("t2", 1, "reserved", 100)
        j.append("t2", 1, "queued", 110)
        j.append("t2", 1, "unknown", 120)
        j.append("t3", 1, "queued", 130)
        j.append("t3", 1, "unsent", 131)
        j.append("t4", 1, "fired", 140)
        j.append("t4", 1, "fired", 999)
        self.assertEqual(j.charges(), {("t2", 1): 120, ("t4", 1): 140})

    def test_replayed_lines_collapse_and_the_window_and_wait(self):
        j = self.journal()
        now = self.clock.t
        for _ in range(3):
            j.append("t1", 1, "queued", now - 10)
        line = (self.work / "state" / "rate.jsonl").read_text()
        (self.work / "state" / "rate.jsonl").write_text(line * 3)
        j = self.journal()
        self.assertEqual(j.times(), [now - 10])
        self.assertEqual(j.wait_seconds(1), 3590)
        self.assertEqual(j.wait_seconds(2), 0)

    def test_a_short_write_still_leaves_a_whole_record_that_survives_restart(self):
        j = self.journal()
        real, shorts = os.write, []

        def short(fd, data):
            if not shorts:
                shorts.append(len(data))
                return real(fd, data[:len(data) - 1])
            return real(fd, data)
        with mock.patch.object(self.lc.os, "write", short):
            j.append("task-a", 1, "queued", self.clock.t)
        self.assertEqual(len(shorts), 1, "the first write was cut short")
        raw = (self.work / "state" / "rate.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"), "the record is complete")
        self.assertTrue(self.journal().charged("task-a", 1), "the charge survives a restart")

    def test_the_first_journal_entry_syncs_the_directory_before_any_record_is_written(self):
        j = self.journal()
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            j.append("task-a", 1, "queued", self.clock.t)
            j.append("task-a", 2, "queued", self.clock.t)
        self.assertEqual(events, ["fsync rate.jsonl", "dir state", "fsync rate.jsonl", "fsync rate.jsonl"],
                         "created and fsynced, its directory fsynced, then the record")

    def test_a_crash_before_the_first_directory_barrier_is_repaired_by_the_restart(self):
        j = self.journal()
        with mock.patch.object(self.lc, "fsync_dir", side_effect=Crash()):
            with self.assertRaises(Crash):
                j.append("task-a", 1, "queued", self.clock.t)
        self.assertEqual((self.work / "state" / "rate.jsonl").read_bytes(), b"", "no record before the barrier")
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            again = self.journal()
        self.assertEqual(events, ["dir state"], "the restart fsyncs the directory that holds the journal")
        again.append("task-a", 1, "queued", self.clock.t)
        self.assertTrue(self.journal().charged("task-a", 1))

    def test_a_journal_a_previous_start_left_without_a_barrier_is_synced_on_open(self):
        j = self.journal()
        j.append("task-a", 1, "queued", self.clock.t)
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            again = self.journal()
        self.assertEqual(events, ["dir state"])
        self.assertTrue(again.charged("task-a", 1), "the charge is read")

    def test_the_event_id_on_disk_is_task_attempt_kind(self):
        j = self.journal()
        j.append("task-a", 2, "queued", 100)
        j.append("task-a", 2, "queued", 200)
        lines = [json.loads(line) for line in (self.work / "state" / "rate.jsonl").read_text().splitlines()]
        self.assertEqual(lines, [{"id": "task-a:2:queued", "task": "task-a", "attempt": 2, "kind": "queued",
                                  "at": 100}], "one line per id; a replay writes nothing")

    def test_a_journal_cut_mid_line_keeps_every_complete_line(self):
        j = self.journal()
        j.append("t1", 1, "queued", self.clock.t)
        j.append("t2", 1, "queued", self.clock.t)
        path = self.work / "state" / "rate.jsonl"
        os.truncate(path, path.stat().st_size - 9)
        j = self.journal()
        self.assertEqual(list(j.charges()), [("t1", 1)])
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_tail_repair_drops_a_torn_last_line(self):
        j = self.journal()
        j.append("t1", 1, "queued", self.clock.t)
        path = self.work / "state" / "rate.jsonl"
        path.write_text(path.read_text() + '{"id": "t2:1:que')
        j = self.journal()
        self.assertEqual(list(j.charges()), [("t1", 1)])
        self.assertTrue(path.read_text().endswith("\n"))
        j.append("t3", 1, "queued", self.clock.t)
        self.assertEqual(len(self.journal().charges()), 2)

    def test_compaction_is_atomic_and_keeps_the_window(self):
        j = self.journal()
        j.append("old", 1, "queued", self.clock.t - 3 * 3600)
        j.append("new", 1, "queued", self.clock.t - 60)
        with mock.patch.object(self.lc, "write_durable", wraps=self.lc.write_durable) as spy:
            j = self.journal()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(list(j.charges()), [("new", 1)])
        self.assertNotIn("old", (self.work / "state" / "rate.jsonl").read_text())


# ── settled ids ──────────────────────────────────────────────────────────────

class SettledIdsTest(Base):
    def test_only_an_unterminated_suffix_is_truncated_and_fsynced(self):
        (self.work / "s").write_text("task-a\ntask-b\ntask-")
        synced = []
        with mock.patch.object(self.lc, "_fsync_file", lambda p: synced.append(p)):
            ids = self.lc.SettledIds(self.work / "s")
        self.assertEqual(ids.ids, {"task-a", "task-b"})
        self.assertEqual(synced, [str(self.work / "s")])
        self.assertEqual((self.work / "s").read_text(), "task-a\ntask-b\n")
        ids.add("task-c")
        self.assertEqual((self.work / "s").read_text(), "task-a\ntask-b\ntask-c\n")

    def test_a_real_short_write_is_truncated_to_the_last_complete_record(self):
        ids = self.lc.SettledIds(self.work / "s")
        for tid in ("task-a", "task-bb", "task-ccc"):
            ids.add(tid)
        size = (self.work / "s").stat().st_size
        os.truncate(self.work / "s", size - 4)
        synced = []
        real = self.lc._fsync_file
        with mock.patch.object(self.lc, "_fsync_file", lambda p: (synced.append(p), real(p))):
            again = self.lc.SettledIds(self.work / "s")
        self.assertEqual(again.ids, {"task-a", "task-bb"})
        self.assertEqual((self.work / "s").read_bytes(), b"task-a\ntask-bb\n")
        self.assertEqual(synced, [str(self.work / "s")])

    def test_any_terminated_malformed_record_is_refused_naming_its_offset(self):
        for text, offset in (("task-a\nnot a task id\ntask-b\n", 7), ("task-a\ntask-c x\n", 7), ("\n", 0)):
            with self.subTest(text=text):
                (self.work / "s").write_text(text)
                with self.assertRaises(self.lc.LedgerError) as e:
                    self.lc.SettledIds(self.work / "s")
                self.assertIn("byte %d" % offset, e.exception.message)
                self.assertEqual((self.work / "s").read_text(), text, "nothing rewritten")
        (self.work / "s").write_text("task-a\nnot a task id\ntask-b\n")
        with self.assertRaises(self.lc.LedgerError) as e:
            self.lc.SettledIds(self.work / "s")
        self.assertIn("byte 7", e.exception.message)
        self.assertEqual(e.exception.code, 2)
        os.unlink(self.work / "s")
        os.mkdir(self.work / "s")
        with self.assertRaises(self.lc.LedgerError):
            self.lc.SettledIds(self.work / "s")

    def test_append_writes_every_byte_then_fsyncs_then_remembers(self):
        ids = self.lc.SettledIds(self.work / "s")
        real_write, order = os.write, []

        def one_byte(fd, data):
            order.append("write")
            return real_write(fd, data[:1])
        with mock.patch.object(self.lc.os, "write", one_byte), \
                mock.patch.object(self.lc.os, "fsync", lambda fd: order.append("fsync")):
            ids.add("task-abc")
        self.assertEqual((self.work / "s").read_text(), "task-abc\n")
        self.assertEqual(order.count("write"), len("task-abc\n"))
        self.assertEqual(order[-2:], ["fsync", "fsync"], "the file, then its directory on creation")
        self.assertIn("task-abc", ids)
        with self.assertRaises(self.lc.LedgerError):
            ids.add("../escape")

    def test_an_append_error_is_a_ledger_error_and_the_id_is_not_remembered(self):
        ids = self.lc.SettledIds(self.work / "s")
        with mock.patch.object(self.lc.os, "write", side_effect=OSError(30, "Read-only file system")):
            with self.assertRaises(self.lc.LedgerError) as e:
                ids.add("task-a")
        self.assertIn(str(self.work / "s"), e.exception.message)
        self.assertNotIn("task-a", ids)

    def test_crash_between_the_settled_claim_and_the_append_is_repaired_once(self):
        life = self.life()
        self.run_to_outcome(life)
        with mock.patch.object(self.lc.SettledIds, "add", side_effect=Crash), self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertEqual(life.claims.load("task-a")["phase"], "SETTLED")
        for _ in range(2):
            self.life().recover()
        self.assertEqual((self.work / "state" / "settled-ids").read_text(), "task-a\n")

    def test_a_marked_work_dir_needs_its_ledger(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.assertEqual((self.work / "state" / "settled-ids").read_bytes(), b"", "created empty at format init")
        os.unlink(self.work / "state" / "settled-ids")
        with self.assertRaises(self.lc.LedgerError) as e:
            self.life()
        self.assertIn("settled-ids is missing", e.exception.message)

    def test_a_failed_ledger_fsync_holds_compaction_until_a_later_start_succeeds(self):
        life = self.life()
        self.run_to_outcome(life)
        path = str(self.work / "state" / "settled-ids")
        real_fsync = os.fsync

        def fail_ledger(fd):
            if os.path.exists(path) and os.fstat(fd).st_ino == os.stat(path).st_ino:
                raise OSError(5, "I/O error")
            return real_fsync(fd)
        with mock.patch.object(self.lc.os, "fsync", fail_ledger):
            with self.assertRaises(self.lc.LedgerError):
                life.publish_and_settle("task-a")
            self.assertEqual(Path(path).read_text(), "task-a\n", "the bytes are visible")
            self.clock.t += 25 * 3600
            with self.assertRaises(self.lc.LedgerError):
                self.life().recover()
            self.assertTrue(life.claims.dir("task-a").exists(), "not compacted while the ledger is not durable")
        self.life().recover()
        self.assertFalse(life.claims.dir("task-a").exists(), "compacted once a start fsyncs the ledger")
        self.assertEqual(Path(path).read_text(), "task-a\n")

    def test_crash_mid_append_is_truncated_and_re_appended_once(self):
        life = self.life()
        self.run_to_outcome(life)
        real_write = os.write

        def half_then_crash(fd, data):
            if data.startswith(b"task-a\n"):
                real_write(fd, data[:3])
                raise Crash()
            return real_write(fd, data)
        with mock.patch.object(self.lc.os, "write", half_then_crash), self.assertRaises(Crash):
            life.publish_and_settle("task-a")
        self.assertEqual((self.work / "state" / "settled-ids").read_text(), "tas")
        self.life().recover()
        self.assertEqual((self.work / "state" / "settled-ids").read_text(), "task-a\n")

    def test_ten_thousand_lines_load_once_and_admission_reads_nothing(self):
        path = self.work / "s"
        path.write_text("".join("task-%d\n" % i for i in range(10000)))
        opened = []

        def counting(real):
            return lambda p, *a, **k: (opened.append(os.path.realpath(os.fspath(p))) if not isinstance(p, int)
                                       else None, real(p, *a, **k))[1]
        with contextlib.ExitStack() as stack:
            import builtins
            for mod, name in ((builtins, "open"), (io, "open"), (os, "open")):
                stack.enter_context(mock.patch.object(mod, name, counting(getattr(mod, name))))
            ids = self.lc.SettledIds(path)
            loaded = opened.count(os.path.realpath(path))
            self.assertTrue(all("task-%d" % i in ids for i in range(0, 10000, 7)))
            self.assertNotIn("task-x", ids)
        self.assertEqual(loaded, 1, "loaded once")
        self.assertEqual(opened.count(os.path.realpath(path)), 1, "no file read per admission")

    def test_recovery_repairs_a_torn_settled_id_before_compaction(self):
        life = self.life()
        self.run_to_outcome(life)
        life.publish_and_settle("task-a")
        path = self.work / "state" / "settled-ids"
        path.write_text(path.read_text()[:-3])
        self.assertEqual(self.lc.SettledIds(path).ids, set(), "the torn record is truncated away")
        self.clock.t += 25 * 3600
        order = []
        real_add, real_remove = self.lc.SettledIds.add, self.lc.ClaimStore.remove
        with mock.patch.object(self.lc.SettledIds, "add", lambda s, t: (order.append("add"), real_add(s, t))), \
                mock.patch.object(self.lc.ClaimStore, "remove", lambda s, t: (order.append("compact"), real_remove(s, t))):
            again = self.life()
            again.recover()
        self.assertEqual(order, ["add", "compact"])
        self.assertIn("task-a", self.lc.SettledIds(path).ids)
        self.assertFalse(again.claims.dir("task-a").exists())

    def test_compaction_never_truncates_the_settled_ids(self):
        life = self.life()
        for tid in ("task-a", "task-b"):
            self.run_to_outcome(life, tid)
            life.publish_and_settle(tid)
        before = (self.work / "state" / "settled-ids").read_bytes()
        self.clock.t += 25 * 3600
        self.life().recover()
        self.assertEqual(os.listdir(self.work / "state" / "claims"), [])
        self.assertEqual((self.work / "state" / "settled-ids").read_bytes(), before)


# ── cancellation and recovery decisions ──────────────────────────────────────

class DecisionTest(Base):
    def test_absent_at_the_final_check_is_never_called(self):
        life = self.life()
        path = self.task()
        attempt = life.prepare("task-a", "i\n")
        os.unlink(path)
        self.assertFalse(life.begin_call("task-a", attempt, path))
        self.assertIsNone(life.claims.load("task-a"), "the claim is dropped")
        self.assertEqual(life.journal.charges(), {})

    def test_removed_after_the_check_before_calling_proceeds_by_its_outcome(self):
        life = self.life()
        path = self.task()
        attempt = life.prepare("task-a", "i\n")
        real = life.mark_calling

        def remove_then_mark(tid, n):
            os.unlink(path)
            real(tid, n)
        life.mark_calling = remove_then_mark
        self.assertTrue(life.begin_call("task-a", attempt, path))
        life.record_outcome("task-a", "queued", "[no-send]\n")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual(list(life.journal.charges()), [("task-a", 1)])

    def restart_with(self, phase_fn, task=True):
        life = self.life()
        path = self.task()
        attempt = life.prepare("task-a", "i\n")
        phase_fn(life, attempt, path)
        if not task:
            os.unlink(path)
        again = self.life()
        again.recover()
        return again

    def test_prepared_with_the_task_present_and_absent_is_left_for_retry(self):
        again = self.restart_with(lambda life, n, p: None)
        self.assertEqual(again.claims.load("task-a")["phase"], "PREPARED")
        self.assertEqual(again.decide("task-a"), ("call", "ABSENT"))
        self.assertNotIn("task-a", again.settled)

    def test_prepared_with_the_task_removed_settles_without_a_call(self):
        again = self.restart_with(lambda life, n, p: None, task=False)
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")
        self.assertIn("task-a", again.settled)
        self.assertFalse((self.results / "task-a.txt").exists())

    def test_not_delivered_with_a_terminal_generation_settles(self):
        def nd(life, n, p):
            life.begin_call("task-a", n, p)
            life.not_delivered("task-a", n, "exit 1")
            (self.results / "task-a.txt").write_text("someone answered\n")
        again = self.restart_with(nd)
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")
        self.assertEqual(again.journal.charges(), {})

    def test_not_delivered_retries_as_the_next_attempt(self):
        life = self.life()
        path = self.task()
        n = life.prepare("task-a", "i\n")
        life.begin_call("task-a", n, path)
        life.not_delivered("task-a", n, "exit 1")
        self.assertEqual(life.prepare("task-a", "i\n"), 2)

    def test_one_attempt_at_a_time(self):
        life = self.life()
        path = self.task()
        n = life.prepare("task-a", "i\n")
        with self.assertRaises(self.lc.AttemptInProgress):
            life.prepare("task-a", "i\n")
        life.begin_call("task-a", n, path)
        with self.assertRaises(self.lc.AttemptInProgress):
            life.prepare("task-a", "i\n")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["attempt"]), ("CALLING", 1), "the open attempt is untouched")
        reserved = [json.loads(line)["id"] for line in (self.work / "state" / "rate.jsonl").read_text().splitlines()]
        self.assertEqual(reserved, ["task-a:1:reserved"])
        life.not_delivered("task-a", n, "exit 1")
        self.assertEqual(life.prepare("task-a", "i\n"), 2, "a proven-unsent attempt closes it")

    def test_parked_states_are_neither_called_nor_settled(self):
        for name in ("claim", "unready", "unknown"):
            with self.subTest(name=name):
                self.setUp()
                life = self.life()
                self.task()
                life.prepare("task-a", "i\n")
                if name == "claim":
                    (self.results / (".task-a.disposing-%d-1-1-1-1-%s-abcd1234" % (os.getpid(), "0" * 64))).write_text("")
                elif name == "unready":
                    (self.results / "task-a.txt").write_text("")
                again = self.life()
                if name == "unknown":
                    with mock.patch.object(self.lc, "classify", return_value="UNKNOWN"):
                        again.recover()
                        self.assertEqual(again.decide("task-a")[0], "park")
                else:
                    again.recover()
                    self.assertEqual(again.decide("task-a")[0], "park")
                self.assertEqual(again.claims.load("task-a")["phase"], "PREPARED")
                self.assertNotIn("task-a", again.settled)

    def test_calling_is_counted_at_the_restart_and_answered_interrupted(self):
        again = self.restart_with(lambda life, n, p: life.begin_call("task-a", n, p))
        self.assertEqual(again.journal.charges(), {("task-a", 1): self.clock.t})
        self.assertEqual((self.results / "task-a.txt").read_text(), "i\n")
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")

    def test_calling_with_a_result_already_present_adds_none(self):
        def calling_answered(life, n, p):
            life.begin_call("task-a", n, p)
            (self.results / "archive").mkdir()
            (self.results / "archive" / "task-a.txt").write_text("done\n")
        again = self.restart_with(calling_answered)
        self.assertFalse((self.results / "task-a.txt").exists())
        self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")

    def test_an_outcome_whose_charge_was_not_journalled_is_charged_once_on_recovery(self):
        life = self.life()
        path = self.task()
        n = life.prepare("task-a", "i\n")
        life.begin_call("task-a", n, path)
        with mock.patch.object(self.lc.RateJournal, "append", side_effect=Crash), self.assertRaises(Crash):
            life.record_outcome("task-a", "queued", "[no-send]\n")
        again = self.life()
        again.recover()
        again.recover()
        self.assertEqual(list(again.journal.charges()), [("task-a", 1)])
        self.assertEqual((self.results / "task-a.txt").read_text(), "[no-send]\n")

    def test_a_result_without_a_call_goes_through_the_same_lifecycle(self):
        life = self.life()
        phases = []
        real = self.lc.ClaimStore.save
        with mock.patch.object(self.lc.ClaimStore, "save", lambda st, rec: (phases.append(rec["phase"]),
                                                                            real(st, rec))):
            self.assertEqual(life.complete_without_call("task-a", "refused\n", "non-owner"), "settled")
        self.assertEqual(phases, ["RESULT_STAGED", "PUBLISHED", "SETTLED"])
        rec = life.claims.load("task-a")
        self.assertEqual((rec["attempt"], rec["outcome"]["kind"], rec["published"]["won"]), (0, "final", True))
        self.assertEqual((self.results / "task-a.txt").read_text(), "refused\n")
        self.assertEqual(life.journal.charges(), {}, "no call, no charge")
        self.assertIn("task-a", life.settled)

    def test_claim_read_and_listing_errors_stop_and_compact_nothing(self):
        life = self.life()
        self.run_to_outcome(life)
        life.publish_and_settle("task-a")
        self.clock.t += 25 * 3600
        claim = life.claims.dir("task-a") / "claim.json"
        self.deny(claim)
        with self.assertRaises(self.lc.StateError) as e:
            self.life().recover()
        self.assertIn("claim.json", e.exception.message)
        self.assertEqual(e.exception.code, 2)
        os.chmod(claim, 0o600)
        claims = self.work / "state" / "claims"
        self.deny(claims)
        with self.assertRaises(self.lc.StateError) as e:
            self.life().recover()
        self.assertIn("claims", e.exception.message)
        os.chmod(claims, 0o700)
        self.assertTrue(claim.exists(), "nothing compacted")

    def test_an_unreadable_claim_parks_and_is_reported(self):
        logs = []
        self.task()
        claim = self.work / "state" / "claims" / "task-a" / "claim.json"
        claim.parent.mkdir(parents=True)
        claim.write_text("{torn")
        life = self.lc.Lifecycle(self.work, clock=self.clock, log=lambda *a: logs.append(" ".join(map(str, a))))
        life.recover()
        self.assertEqual(life.decide("task-a")[0], "park", "never called")
        self.assertTrue([line for line in logs if str(claim) in line], logs)
        self.assertEqual(claim.read_text(), "{torn", "kept as it was")
        self.assertNotIn("task-a", life.settled)
        self.assertFalse((self.results / "task-a.txt").exists())


# ── structure ────────────────────────────────────────────────────────────────

class SpyMixin:
    """A write spy over the work dir, attributing each write to the module boundary that made it."""

    BOUNDARIES = ("task_lifecycle.py", "relay_loop.py")

    @contextlib.contextmanager
    def write_spy(self, work):
        """Every filesystem write under `work`, attributed to the module boundary that made it: the
        innermost frame in the owner, the shared loop or an adapter (generic helpers are skipped)."""
        pkg = str(PKG_ROOT / "ag2_sparrow") + os.sep
        work = os.path.realpath(work)
        seen = set()

        def owner():
            f = sys._getframe(2)
            while f is not None:
                name = f.f_code.co_filename
                if name.startswith(pkg) and (os.path.basename(name) in self.BOUNDARIES or
                                             os.sep + "adapters" + os.sep in name):
                    return os.path.relpath(name, pkg)
                f = f.f_back
            return None

        def note(path, dir_fd=None):
            try:
                path = os.fspath(path)
                if dir_fd is not None and not os.path.isabs(path):
                    path = os.path.join(fd_path(dir_fd), path)
                full = os.path.realpath(path)
            except TypeError:
                return
            if full.startswith(work + os.sep):
                who = owner()
                if who:
                    seen.add((who, os.path.relpath(full, work)))

        def wrap(fn, writes):
            def spy(*a, **k):
                if writes(*a, **k):
                    for p in (a[:2] if fn.__name__ in ("replace", "rename", "link") else
                              a[1:2] if fn.__name__ == "symlink" else a[:1]):
                        note(p, k.get("dir_fd"))
                return fn(*a, **k)
            return spy
        def mode_writes(path, mode="r", *a, **k):
            return any(c in mode for c in "wax+")

        def flags_write(path, flags, *a, **k):
            return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        always = lambda *a, **k: True  # noqa: E731
        import builtins
        import io as _io
        import shutil as _shutil
        patches = [mock.patch.object(builtins, "open", wrap(builtins.open, mode_writes)),
                   mock.patch.object(_io, "open", wrap(_io.open, mode_writes)),
                   mock.patch.object(os, "open", wrap(os.open, flags_write))]
        patches += [mock.patch.object(os, n, wrap(getattr(os, n), always))
                    for n in ("replace", "rename", "link", "symlink", "truncate", "unlink", "mkdir", "rmdir")]
        patches.append(mock.patch.object(_shutil, "rmtree", wrap(_shutil.rmtree, always)))
        # The no-replace rename goes through a kernel call, not os.rename.
        from ag2_sparrow import undelivered_quarantine
        real_noreplace = undelivered_quarantine.rename_noreplace
        patches.append(mock.patch.object(undelivered_quarantine, "rename_noreplace",
                                         lambda src, dst, log=None: (note(src), note(dst),
                                                                     real_noreplace(src, dst, log))[2]))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield seen

    @staticmethod
    def shape(path):
        path = re.sub(r"task-[A-Za-z0-9-]+", "<task>", path)
        path = re.sub(r"\.\d+\.[0-9a-f]{6,8}\.tmp$|\.\d+\.tmp$", ".<tmp>", path)
        path = re.sub(r"\.probe-\d+-[0-9a-f]{8}", ".probe", path)
        return re.sub(r"[0-9a-f]{64}", "<key>", path)

    def fake_claude(self, d):
        claude = Path(d) / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text("#!%s\nimport sys\nsys.stdin.read()\nprint('{\"ok\": true, \"url\": \"u\"}')\n"
                          % sys.executable)
        claude.chmod(0o755)
        return claude

    def write_task(self, work, tid, channel="!r:x"):
        p = Path(work) / "tasks" / (tid + ".txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("id: %s\nchannel_id: %s\naccess_tier: owner\npriority: normal\ntask: hi\n" % (tid, channel))

    def by_module(self, seen, module):
        return {self.shape(p) for who, p in seen if who.endswith(module)}


class StructureTest(SpyMixin, unittest.TestCase):
    """The cloud-session adapter writes under results/ and state/claims/ only through the owner."""

    def test_the_cloud_session_adapter_writes_only_through_the_owner(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            claude = self.fake_claude(d)
            err = io.StringIO()
            env = {"PATH": "/usr/bin:/bin", "HOME": d}
            argv = ["--work", str(work), "--session-id", "session_X", "--claude", str(claude), "--poll", "0.02",
                    "--idle-exit", "0.1"]
            work.mkdir()
            with self.write_spy(work) as seen, contextlib.redirect_stderr(err):
                self.assertEqual(sa.main(argv, env), 0, err.getvalue())
                self.write_task(work, "task-a")
                self.write_task(work, "task-b", channel="")
                self.assertEqual(sa.main(argv, env), 0, err.getvalue())
        self.assertEqual(self.by_module(seen, os.path.join("claude_cloud_session", "session_adapter.py")),
                         {"state", "tasks", "results"}, "the adapter itself only creates its directories")
        self.assertTrue({"state/settled-ids", "state/claims", "state/format", "state/.format.<tmp>"}
                        <= self.by_module(seen, "task_lifecycle.py"), "first init is the owner's")
        owners = {who for who, p in seen if p.startswith(("results" + os.sep, os.path.join("state", "claims")))}
        self.assertEqual(owners, {"task_lifecycle.py"}, "results/ and state/claims/ only through the owner")
        self.assertTrue({"results/<task>.txt", "state/claims/<task>/claim.json", "state/claims/<task>/body",
                         "state/settled-ids", "state/rate.jsonl"} <= self.by_module(seen, "task_lifecycle.py"))


    def test_no_module_but_task_io_names_the_lock_or_the_pidfile(self):
        found = {}
        paths = [p for p in sorted((PKG_ROOT / "ag2_sparrow").rglob("*.py"))
                 if "__pycache__" not in p.parts and p.name not in ("task_io.py", "holder_lock.py")]
        paths += [REPO_SRC / "task_lifecycle.py"] if (REPO_SRC / "task_lifecycle.py").exists() else []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            hits = [n.value[:60] for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and "connector.pid" in n.value]
            hits += [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                     and "holder_lock" in ast.unparse(n)]
            hits += [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "holder_lock"]
            if hits:
                found[str(path.relative_to(PKG_ROOT.parents[1]))] = hits
        self.assertEqual(found, {})


# A pre-holder_lock revision's own pidfile, under the work root; the shared lock keeps nothing there.
LOCK_FILES = {"state/connector.pid", "state/.connector.pid.<tmp>"}


class BoundaryGuardTest(SpyMixin, unittest.TestCase):
    """Guards that hold before and after the owner lands, so they are not gated: routine and Codex
    write where they always have, and nothing outside the cloud-session adapter reaches the owner."""

    def test_the_routine_adapter_writes_only_through_the_shared_loop(self):
        def opener(req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError())
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            self.write_task(work, "task-a")
            argv = ["--work", str(work), "--trigger-id", "trig_01SPY", "--once", "1", "--poll", "0.01",
                    "--max-retries", "0"]
            with self.write_spy(work) as seen, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ra.main(argv, {"CLAUDE_ROUTINE_TOKEN": "t"}, opener), 0)
        self.assertEqual(self.by_module(seen, os.path.join("claude_routine", "routine_adapter.py")) - LOCK_FILES,
                         {"state", "results", "tasks"})
        self.assertEqual(self.by_module(seen, "relay_loop.py"),
                         {"results", "state", "results/<task>.txt", "results/.<task>.txt.staged",
                          "results/..<task>.txt.staged.<tmp>", "state/done", "state/.done.<tmp>",
                          "state/fires.json", "state/.fires.json.<tmp>"})

    def test_the_codex_adapter_writes_where_it_always_has(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            self.write_task(work, "task-a")
            with self.write_spy(work) as seen, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cc.main(["--server", "stub", "--work", str(work), "--once", "1", "--poll", "0.01"]), 0)
        self.assertEqual(self.by_module(seen, os.path.join("codex", "codex_connector.py")) - LOCK_FILES,
                         {"state", "results", "tasks", "results/<task>.txt",
                          "results/.<task>.txt.<tmp>", "state/done", "state/.done.<tmp>", "state/threads",
                          "state/threads/<key>", "state/threads/.<key>.<tmp>", "state/metrics.jsonl",
                          "state/app-server-stub.stderr"})

    def strings(self, tree):
        doc = tree.body[0].value if tree.body and isinstance(tree.body[0], ast.Expr) else None
        return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n is not doc]

    def test_no_delegating_adapter_names_the_owners_records(self):
        for rel in ("claude_cloud_session/session_adapter.py", "claude_routine/routine_adapter.py"):
            tree = ast.parse((ADAPTERS / rel).read_text())
            self.assertFalse([s for s in self.strings(tree) if "claims" in s or "rate.jsonl" in s], rel)

    @staticmethod
    def references(path):
        """Every place a module imports, names or spells task_lifecycle."""
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [a.name for a in node.names if "task_lifecycle" in a.name]
            elif isinstance(node, ast.ImportFrom):
                found += [(node.module or "") + ":" + a.name for a in node.names
                          if "task_lifecycle" in (node.module or "") + a.name]
            elif isinstance(node, ast.Name) and "task_lifecycle" in node.id:
                found.append(node.id)
            elif isinstance(node, ast.Attribute) and "task_lifecycle" in node.attr:
                found.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "task_lifecycle" in node.value:
                found.append(node.value[:60])
        return found

    # The one named exception: the claude-local Mod's lifecycle CLI. It is that adapter's only
    # importer; every other claude_code module reaches the lifecycle through it.
    LOCAL_INBOX = ("adapters", "claude_code", "inbox.py")

    @staticmethod
    def production_modules():
        """Every production module outside the cloud-session adapter, the local inbox CLI and the owner."""
        pkg = PKG_ROOT / "ag2_sparrow"
        for path in sorted(pkg.rglob("*.py")):
            rel = path.relative_to(pkg)
            if "__pycache__" in rel.parts or rel.parts[:2] == ("adapters", "claude_cloud_session"):
                continue
            if rel.name == "task_lifecycle.py" or rel.parts == BoundaryGuardTest.LOCAL_INBOX:
                continue
            yield path

    def test_the_local_inbox_cli_is_the_one_named_exception(self):
        inbox = PKG_ROOT / "ag2_sparrow" / Path(*self.LOCAL_INBOX)
        self.assertTrue(self.references(inbox), "the exception is real: the inbox CLI imports the owner")
        names = {str(p.relative_to(PKG_ROOT)) for p in self.production_modules()}
        self.assertIn("ag2_sparrow/adapters/claude_code/launcher.py", names, "the rest of the adapter is still scanned")
        self.assertIn("ag2_sparrow/adapters/claude_code/hold.py", names)

    def test_nothing_outside_the_cloud_session_adapter_reaches_the_owner(self):
        modules = list(self.production_modules())
        names = {str(p.relative_to(PKG_ROOT)) for p in modules}
        for must in ("ag2_sparrow/relay_loop.py", "ag2_sparrow/adapters/claude_routine/routine_adapter.py",
                     "ag2_sparrow/adapters/codex/codex_connector.py", "ag2_sparrow/adapters/hatch/cli.py",
                     "ag2_sparrow/task_io.py", "ag2_sparrow/task_priority.py", "ag2_sparrow/result_disposal.py"):
            self.assertIn(must, names)
        self.assertEqual({str(p.relative_to(PKG_ROOT)): self.references(p) for p in modules
                          if self.references(p)}, {})

    def test_the_scan_catches_a_reference(self):
        with tempfile.TemporaryDirectory() as d:
            for i, text in enumerate(("from ag2_sparrow import task_lifecycle\n",
                                      "from .. import task_lifecycle as lc\n",
                                      "import importlib\nm = importlib.import_module('ag2_sparrow.task_lifecycle')\n",
                                      "x = owner.task_lifecycle\n")):
                p = Path(d) / ("m%d.py" % i)
                p.write_text(text)
                self.assertTrue(self.references(p), text)
            (Path(d) / "clean.py").write_text("import os\n")
            self.assertEqual(self.references(Path(d) / "clean.py"), [])

    def test_the_owner_imports_no_adapter(self):
        for path in (REPO_SRC / "task_lifecycle.py", PKG_ROOT / "ag2_sparrow" / "task_lifecycle.py"):
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            imported = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            self.assertFalse([i for i in imported if "adapters" in i], imported)

    def test_vendored_copy_matches_src(self):
        if (REPO_SRC / "task_lifecycle.py").exists():
            self.assertEqual((REPO_SRC / "task_lifecycle.py").read_text(),
                             (PKG_ROOT / "ag2_sparrow" / "task_lifecycle.py").read_text())


class ErrorBranchTest(Base):
    """The error and edge branches of the owner's helpers, driven with real files."""

    def test_finish_staged_completes_only_with_the_recorded_verifying_body(self):
        lc = self.lc
        life = lc.Lifecycle(self.work, identity={"pid": 1, "start": 1}, lineage=lambda *a: True, log=lambda *a: None)
        self.assertEqual(life.finish_staged("nope"), "parked")
        store = life.claims
        rec = {"schema": 1, "task_id": "t", "attempt": 1, "phase": lc.TURN_CONFIRMED, "claim_mode": lc.LOCAL,
               "turn_id": "turn-1", "generation": {"pid": 1, "start": 1}}
        store.save(rec)
        self.assertEqual(life.finish_staged("t"), "parked", "no recorded descriptor")
        body = b"answer\n"
        identity = life._identity_of(rec, "turn-1")
        name, _ = life._blob_name(rec, identity)
        store.save(dict(rec, staged={"identity": identity, "length": len(body), "sha256": lc._sha256(body), "blob": name}))
        self.assertEqual(life.finish_staged("t"), "parked", "the blob is missing")
        (store.dir("t") / name).write_bytes(body)
        self.assertEqual(life.finish_staged("t"), "staged")
        self.assertEqual(store.load("t")["phase"], lc.RESULT_STAGED)

    def test_a_rename_that_landed_is_committed_and_a_failed_write_is_io(self):
        lc = self.lc
        store = lc.ClaimStore(self.work / "claims")
        with mock.patch.object(lc, "fsync_dir", side_effect=OSError(errno.EIO, "dir fsync")):
            with self.assertRaises(lc.RenamedNotDurable):
                lc.write_durable(self.work / "f", b"x")
            self.assertEqual((self.work / "f").read_bytes(), b"x", "the rename landed")
            (self.work / "claims" / "t").mkdir(parents=True)
            with self.assertRaises(lc.StateCommitted):
                store.save({"task_id": "t", "phase": "PREPARED"})
            with self.assertRaises(lc.StateCommitted):
                store.stage("t", b"body")
        with mock.patch.object(lc, "write_durable", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(lc.StateIOError):
                store.save({"task_id": "t", "phase": "PREPARED"})
            with self.assertRaises(lc.StateIOError):
                store.stage("t", b"body")

    def test_helpers_report_missing_paths_and_log_to_stderr(self):
        lc = self.lc
        self.assertIsNone(lc._lstat(self.work / "nope"))
        self.assertIsNotNone(lc._lstat(self.work))
        self.assertFalse(lc._is_type(self.work / "nope", "dir"))
        (self.work / "d").mkdir()
        (self.work / "d" / "f").write_text("x")
        self.assertEqual(lc._first_entry(str(self.work / "d")), "f")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            lc._log("hello", 1)
        self.assertIn("hello 1", err.getvalue())

    def test_a_flat_load_finds_its_siblings_through_the_path(self):
        spec = importlib.util.spec_from_file_location("task_lifecycle_flat", self.lc.__file__)
        flat = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(flat)
        self.assertTrue(callable(flat.Lifecycle))

    def test_a_failed_replace_leaves_no_temp_behind(self):
        target = self.work / "out"
        with mock.patch.object(self.lc.os, "replace", side_effect=OSError(errno.EIO, "boom")):
            with self.assertRaises(OSError):
                self.lc.write_durable(target, b"x")
        self.assertEqual(os.listdir(self.work), [])

    def test_a_marked_state_that_is_a_link_is_refused(self):
        real = self.work / "real"
        (real / "claims").mkdir(parents=True)
        (real / "settled-ids").write_text("")
        (real / "format").write_text(self.lc.FORMAT + "\n")
        os.symlink(real, self.work / "state")
        self.assertIn("is not a directory", self.lc.prepare_work_dir(str(self.work)))

    def test_the_journal_skips_a_bad_line_and_reports_a_charge(self):
        life = self.life()
        life.journal.append("task-a", 1, "unsent")
        life.journal.append("task-a", 2, "queued")
        with open(life.journal.path, "ab") as f:
            f.write(b"not json\n")
        again = self.lc.RateJournal(life.journal.path, self.clock)
        self.assertEqual(len(again.events), 2)
        self.assertTrue(again.charged("task-a", 2))
        self.assertFalse(again.charged("task-a", 1))

    def test_the_ledger_fails_closed_and_adds_once(self):
        path = self.work / "ids"
        path.write_bytes(b"\xff\xfe\n")
        with self.assertRaises(self.lc.LedgerError):
            self.lc.SettledIds(path)
        path.write_bytes(b"task-a\ntask-b")
        with mock.patch.object(self.lc.os, "truncate", side_effect=OSError(errno.EPERM, "no")):
            with self.assertRaises(self.lc.LedgerError):
                self.lc.SettledIds(path)
        path.write_bytes(b"task-a\n")
        ids = self.lc.SettledIds(path)
        ids.add("task-a")
        self.assertEqual(path.read_bytes(), b"task-a\n", "a recorded id is not appended twice")

    def test_a_claim_that_names_another_task_is_unreadable(self):
        store = self.lc.ClaimStore(self.work / "claims")
        (self.work / "claims" / "task-a").mkdir(parents=True)
        claim = self.work / "claims" / "task-a" / "claim.json"
        claim.write_text("[]")
        with self.assertRaises(self.lc.ClaimUnreadable):
            store.load("task-a")
        claim.write_text('{"task_id": "task-b", "phase": "prepared"}')
        with self.assertRaises(self.lc.ClaimUnreadable):
            store.load("task-a")
        claim.write_text('{"task_id": "task-a", "phase": "prepared"}')
        self.assertEqual(store.load("task-a")["phase"], "prepared")

    def test_the_stage_reports_io_errors_and_tolerates_absence(self):
        store = self.lc.ClaimStore(self.work / "claims")
        with mock.patch.object(self.lc, "write_durable", side_effect=OSError(errno.EIO, "x")):
            with self.assertRaises(self.lc.StateError):
                store.stage("task-a", b"b")
        with self.assertRaises(self.lc.StateError):
            store.staged("task-a")
        store.unstage("task-a")
        store.remove("task-a")
        store.stage("task-a", b"b")
        with mock.patch.object(self.lc.os, "unlink", side_effect=PermissionError(errno.EACCES, "x")):
            with self.assertRaises(self.lc.StateError):
                store.unstage("task-a")
        with mock.patch.object(self.lc.shutil, "rmtree", side_effect=PermissionError(errno.EACCES, "x")):
            with self.assertRaises(self.lc.StateError):
                store.remove("task-a")

    def test_a_body_hold_check_that_cannot_read_is_false(self):
        life = self.life()
        with mock.patch.object(self.lc.local_task_protocol, "iter_result_candidates",
                               side_effect=OSError(errno.EIO, "x")):
            self.assertFalse(life._holds_body("task-a", b"b"))

    def test_a_foreign_exists_error_is_not_a_taken_name(self):
        life = self.life()
        life.claims.stage("task-a", b"b\n")
        life._save({"schema": 1, "task_id": "task-a", "attempt": 0, "created_at": 0}, phase=self.lc.RESULT_STAGED)
        with mock.patch.object(self.lc.undelivered_quarantine, "move_private",
                               side_effect=FileExistsError(errno.ENOTSUP, "x")):
            with self.assertRaises(FileExistsError):
                with self.lc.result_disposal.locked(life.results):
                    life._publish(life.claims.load("task-a"))

    def test_a_published_file_that_differs_from_the_stage_is_logged(self):
        lines = []
        life = self.lc.Lifecycle(self.work, clock=self.clock, log=lambda *a: lines.append(" ".join(map(str, a))))
        life.claims.stage("task-a", b"b\n")
        life._save({"schema": 1, "task_id": "task-a", "attempt": 0, "created_at": 0}, phase=self.lc.RESULT_STAGED)
        with mock.patch.object(self.lc.Lifecycle, "read_final", return_value=b"other"):
            self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertTrue(any("differs from the stage" in l for l in lines))

    def test_recover_ignores_a_claim_dir_with_no_record(self):
        life = self.life()
        (life.claims.root / "task-a").mkdir(parents=True)
        life.recover()
        self.assertTrue((life.claims.root / "task-a").is_dir())


# ── claim modes, the turn transitions and the staged commit ───────────────────

GEN = {"pid": 2 ** 22 - 1, "start": "2026-01-01T00:00:00Z"}
OTHER = {"pid": 2 ** 22 - 2, "start": "2026-01-01T00:00:01Z"}


def main_lifecycle():
    """The pre-claude-local task_lifecycle checked in as a fixture, as its own module; a missing
    fixture is an error, never a skip."""
    path = PKG_ROOT.parents[1] / "tests" / "fixtures" / "old_main" / "task_lifecycle_pre_local.py"
    spec = importlib.util.spec_from_file_location("task_lifecycle_main", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def snapshot(root):
    """Every file under `root` with its bytes, so "nothing changed" is a byte comparison."""
    out = {}
    for base, _, names in os.walk(root):
        for name in names:
            p = Path(base) / name
            try:
                out[str(p.relative_to(root))] = p.read_bytes()
            except OSError:
                out[str(p.relative_to(root))] = None
    return out


class LocalClaimTest(Base):
    """Claim modes, authorize_call, confirm_turn, stage_result's commit order, the parked rule,
    mode-enforced recovery, the state allowlist and the format fence."""

    def setUp(self):
        super().setUp()
        self.lineage = {}

    def life(self, lineage=None, identity=None):
        lc = self.lc.Lifecycle(self.work, clock=self.clock, log=self.logged,
                               identity=identity, lineage=lineage or self.lineage.get)
        return lc

    def logged(self, *a):
        self.logs = getattr(self, "logs", [])
        self.logs.append(" ".join(map(str, a)))

    def local_dir(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.lc.enable_local_format(str(self.work))

    def claimed(self, life, tid="task-a", gen=GEN):
        self.task(tid)
        return life.prepare(tid, "interrupted\n", mode=self.lc.LOCAL, generation=gen)

    def confirmed(self, life, tid="task-a", gen=GEN, text="hello"):
        attempt = self.claimed(life, tid, gen)
        h = self.lc._sha256(text.encode())
        nonce = life.authorize_call(tid, attempt, text_hash=h)
        life.confirm_turn(tid, "turn-1", nonce, h)
        return attempt

    def claim_bytes(self, tid="task-a"):
        return (self.work / "state" / "claims" / tid / "claim.json").read_bytes()

    # each recovery and fence branch, on both copies

    def test_generation_recovery_settles_or_keeps_each_phase_exactly_once(self):
        self.local_dir()
        life = self.life()
        for tid in ("task-gone", "task-done", "task-wait", "task-call", "task-lost", "task-new", "task-old"):
            self.claimed(life, tid)
        (self.work / "tasks" / "task-gone.txt").unlink()
        self.results.mkdir(exist_ok=True)
        (self.results / "task-done.txt").write_text("already\n")
        for tid, phase in (("task-call", "CALLING"), ("task-lost", "OUTCOME_UNKNOWN"), ("task-new", "SOMETHING_NEW")):
            life.claims.save(dict(life.claims.load(tid), phase=phase))
        lost = {"identity": "i", "length": 1, "sha256": "0" * 64, "blob": "blob.1.absent"}
        life.claims.save(dict(life.claims.load("task-lost"), staged=lost))
        old = self.clock.t - self.lc.COMPACT_AFTER - 1
        life.claims.save(dict(life.claims.load("task-old"), phase="SETTLED", settled_at=old))
        self.logs = []
        again = self.life(lineage=lambda pid, start: False)
        again.recover(generation=GEN, proof=False)
        phase = {t: (again.claims.load(t) or {}).get("phase") for t in
                 ("task-gone", "task-done", "task-wait", "task-call", "task-lost", "task-new", "task-old")}
        self.assertEqual(phase, {"task-gone": "SETTLED", "task-done": "SETTLED", "task-wait": "PREPARED",
                                 "task-call": "SETTLED", "task-lost": "RESULT_STAGED", "task-new": "SOMETHING_NEW",
                                 "task-old": None})
        self.assertEqual((self.results / "task-call.txt").read_bytes(), b"interrupted\n", "counted, never resent")
        self.assertFalse((self.results / "task-lost.txt").exists(), "no verified staged body: parked")
        self.assertIn("task-old", again.settled, "re-recorded before it was compacted")
        self.assertIn("a phase this reader does not know, left alone -> task-new SOMETHING_NEW", self.logs)
        self.assertIn("no verified staged body; parked -> task-lost", self.logs)

    def test_the_cloud_recovery_leaves_an_unknown_phase_alone(self):
        life = self.life()
        self.task()
        life.prepare("task-a", "i\n")
        life.claims.save(dict(life.claims.load("task-a"), phase="SOMETHING_NEW"))
        self.logs = []
        self.life(lineage=lambda pid, start: False).recover()
        self.assertEqual(life.claims.load("task-a")["phase"], "SOMETHING_NEW")
        self.assertIn("a phase this reader does not know, left alone -> task-a SOMETHING_NEW", self.logs)

    def test_the_sweep_survives_an_unlistable_dir_a_vanished_temp_and_a_failed_unlink(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        d = self.work / "state" / "claims" / "task-a"
        temp = d / (".task-a.1.%s.%d.deadbeef.tmp" % ("a" * 24, 2 ** 22 - 1))
        temp.write_bytes(b"x")
        real_listdir, real_unlink = os.listdir, os.unlink
        self.logs = []
        dead = self.life(lineage=lambda pid, start: False)
        with mock.patch.object(self.lc.os, "listdir", lambda p=".": (_ for _ in ()).throw(OSError(errno.EIO, "io"))
                               if str(p) == str(d) else real_listdir(p)):
            dead._sweep_temps("task-a", self.clock.t)
        with mock.patch.object(self.lc, "_lstat", return_value=None):
            dead._sweep_temps("task-a", self.clock.t)
        self.assertTrue(temp.exists(), "unlisted or vanished: nothing removed")

        def unlink(p, **k):
            if str(p) == str(temp):
                raise OSError(errno.EBUSY, "busy")
            return real_unlink(p, **k)
        with mock.patch.object(self.lc.os, "unlink", unlink):
            dead._sweep_temps("task-a", self.clock.t)
        self.assertTrue(temp.exists())
        self.assertTrue(any(x.startswith("a leftover temp could not be swept -> task-a") for x in self.logs), self.logs)

    def test_an_unknown_format_marker_is_refused_at_startup_and_by_the_fence(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        (self.work / "state" / "format").write_text("ag2-sparrow-work-v99\n")
        self.assertIn("says 'ag2-sparrow-work-v99'", self.lc.prepare_work_dir(str(self.work)))
        with self.assertRaises(self.lc.StateError):
            self.lc.enable_local_format(str(self.work))
        (self.work / "state" / "format").unlink()
        self.assertIsNone(self.life()._format(), "an absent marker reads as no format")

    def test_the_fence_reports_a_marker_it_cannot_write(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        with mock.patch.object(self.lc, "write_durable", side_effect=OSError(errno.EROFS, "read-only")):
            with self.assertRaises(self.lc.StateError) as e:
                self.lc.enable_local_format(str(self.work))
        self.assertIn("read-only", str(e.exception))

    def test_a_blob_that_is_not_a_file_is_a_state_error_never_absent(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        (self.work / "state" / "claims" / "task-a" / "blob.x").mkdir()
        self.assertIsNone(life.claims.blob("task-a", "blob.none"))
        with self.assertRaises(self.lc.StateError):
            life.claims.blob("task-a", "blob.x")

    def test_staging_parks_a_claim_past_its_turn_and_a_result_that_reads_back_wrong(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        rec = life.claims.load("task-a")
        life.claims.save(dict(rec, phase="OUTCOME_UNKNOWN"))
        self.logs = []
        self.assertEqual(life.stage_result("task-a", b"ok\n", "turn-1"), "parked")
        self.assertIn("stage refused: claim is OUTCOME_UNKNOWN -> task-a", self.logs)
        life.claims.save(rec)
        with mock.patch.object(life, "_verified", return_value=None):
            self.assertEqual(life.stage_result("task-a", b"ok\n", "turn-1"), "parked")
        self.assertIn("the result read back does not verify; parked -> task-a", self.logs)
        self.assertEqual(life.claims.load("task-a")["phase"], "TURN_CONFIRMED")

    # claim modes and the PREPARED refusal

    def test_claims_carry_their_mode_and_a_legacy_claim_re_prepares_as_today(self):
        life = self.life(identity={"pid": 4242, "start": "s"})
        self.task()
        life.prepare("task-a", "i\n")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["claim_mode"], rec["claim_version"], rec["writer"]),
                         ("cloud", self.lc.CLAIM_VERSION, {"pid": 4242, "start": "s"}))
        self.assertNotIn("generation", rec)
        legacy = {k: v for k, v in rec.items() if k not in ("claim_mode", "claim_version", "writer")}
        life.claims.save(legacy)
        again = self.life(lineage=lambda pid, start: True)
        self.assertEqual(again.prepare("task-a", "i\n"), 1, "no writer recorded: re-prepared as today")
        self.assertEqual(again.claims.load("task-a")["writer"], again.identity)

    def test_a_prepared_cloud_claim_is_refused_while_its_writer_is_live_or_unobserved(self):
        a = self.life(identity={"pid": 4242, "start": "s"})
        self.task()
        a.prepare("task-a", "i\n")
        before = self.claim_bytes()
        for seen in (True, None):
            with self.subTest(writer=seen):
                b = self.life(lineage=lambda pid, start, seen=seen: seen, identity={"pid": 4343, "start": "t"})
                with self.assertRaises(self.lc.AttemptInProgress):
                    b.prepare("task-a", "i\n")
                self.assertEqual(self.claim_bytes(), before, "refused with the claim's bytes unchanged")
        b = self.life(lineage=lambda pid, start: False, identity={"pid": 4343, "start": "t"})
        self.assertEqual(b.prepare("task-a", "i\n"), 1, "a dead writer's claim is re-prepared")
        self.assertEqual(b.claims.load("task-a")["writer"], {"pid": 4343, "start": "t"})
        self.assertEqual(a.identity, {"pid": 4242, "start": "s"})
        own = self.life(lineage=lambda pid, start: True, identity={"pid": 4343, "start": "t"})
        self.assertEqual(own.prepare("task-a", "i\n"), 1, "our own writer identity is never 'elsewhere'")

    def test_the_default_liveness_admits_only_a_verified_dead_pid(self):
        self.assertIs(self.lc._dead_or_unobserved(2 ** 22 - 1), False)
        self.assertIsNone(self.lc._dead_or_unobserved(os.getpid()), "live is unobserved by default, never admitted")
        self.assertIsNone(self.lc._dead_or_unobserved(2 ** 70))
        life = self.lc.Lifecycle(self.work, clock=self.clock, log=quiet)
        self.assertIs(life.lineage, self.lc._dead_or_unobserved)
        self.assertEqual(life.identity, {"pid": os.getpid(), "start": None})

    def test_a_local_claim_needs_the_fence_and_binds_to_its_generation(self):
        life = self.life()
        self.task()
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        with self.assertRaises(self.lc.StateError) as e:
            life.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=GEN)
        self.assertIn("claude-local/1", e.exception.message)
        self.assertIsNone(life.claims.load("task-a"), "no claim written without the fence")
        with self.assertRaises(ValueError):
            life.prepare("task-a", "i\n", mode=self.lc.LOCAL)
        with self.assertRaises(ValueError):
            life.prepare("task-a", "i\n", mode="other")
        self.lc.enable_local_format(str(self.work))
        self.assertEqual((self.work / "state" / "format").read_text(), "claude-local/1\n")
        self.assertTrue((self.work / "state" / "claims.lock").is_file())
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "the bumped dir is served by this binary")
        self.assertEqual(life.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=GEN), 1)
        rec = life.claims.load("task-a")
        self.assertEqual((rec["claim_mode"], rec["generation"]), ("local", GEN))
        self.assertNotIn("writer", rec)

    def test_cli_a_claims_and_exits_while_the_consumer_lives_so_cli_b_refuses_with_unchanged_bytes(self):
        self.local_dir()
        a = self.life(lineage=lambda pid, start: True)
        self.claimed(a)
        before = self.claim_bytes()
        b = self.life(lineage=lambda pid, start: True)
        with self.assertRaises(self.lc.AttemptInProgress):
            b.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=OTHER)
        self.assertEqual(self.claim_bytes(), before)
        c = self.life(lineage=lambda pid, start: None)
        with self.assertRaises(self.lc.AttemptInProgress):
            c.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=OTHER)
        self.assertEqual(self.claim_bytes(), before, "unobserved refuses too")
        same = self.life(lineage=lambda pid, start: True)
        self.assertEqual(same.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=GEN), 1,
                         "the same generation gets its attempt back")
        self.assertEqual(self.claim_bytes(), before, "and nothing is rewritten")
        dead = self.life(lineage=lambda pid, start: False)
        self.assertEqual(dead.prepare("task-a", "i\n", mode=self.lc.LOCAL, generation=OTHER), 1)
        self.assertEqual(dead.claims.load("task-a")["generation"], OTHER, "a dead generation's claim is re-prepared")

    # authorize_call and confirm_turn

    def test_two_authorize_calls_yield_one_token(self):
        self.local_dir()
        life = self.life()
        attempt = self.claimed(life)
        h = self.lc._sha256(b"hello")
        self.assertIsNone(life.authorize_call("task-a", attempt + 1, text_hash=h), "another attempt never authorizes")
        self.assertEqual(life.claims.load("task-a")["phase"], "PREPARED")
        token = life.authorize_call("task-a", attempt, text_hash=h)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["call"]["nonce"], rec["call"]["text_hash"]), ("CALLING", token, h))
        self.assertIsNone(life.authorize_call("task-a", attempt, text_hash=h), "CALLING never authorizes a second submit")
        self.assertIsNone(self.life().authorize_call("task-a", attempt, text_hash=h), "from another CLI either")
        self.assertEqual(life.claims.load("task-a")["call"]["nonce"], token, "the one token stands")
        self.assertIsNone(life.authorize_call("task-b", 1), "no claim, no token")

    def test_confirm_turn_validates_the_nonce_and_hash_and_records_the_turn(self):
        self.local_dir()
        life = self.life()
        attempt = self.claimed(life)
        h = self.lc._sha256(b"hello")
        with self.assertRaises(self.lc.TurnRefused):
            life.confirm_turn("task-a", "turn-1", "x", h)
        token = life.authorize_call("task-a", attempt, text_hash=h)
        before = self.claim_bytes()
        for nonce, hsh, turn in ((token[::-1], h, "turn-1"), (token, self.lc._sha256(b"other"), "turn-1"), (token, h, "")):
            with self.subTest(nonce=nonce != token, hash=hsh != h, turn=turn):
                with self.assertRaises(self.lc.TurnRefused):
                    life.confirm_turn("task-a", turn, nonce, hsh)
                self.assertEqual(self.claim_bytes(), before, "refused with the claim untouched")
        life.confirm_turn("task-a", "turn-1", token, h)
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["turn_id"]), ("TURN_CONFIRMED", "turn-1"))
        self.assertEqual(list(life.journal.charges()), [("task-a", 1)], "the turn is the charge")
        with self.assertRaises(self.lc.TurnRefused):
            life.confirm_turn("task-a", "turn-2", token, h)
        self.assertEqual(life.claims.load("task-a")["turn_id"], "turn-1", "one confirmation per call")
        legacy = self.life()
        self.task("task-b")
        n = legacy.prepare("task-b", "i\n")
        legacy.mark_calling("task-b", n)
        with self.assertRaises(self.lc.TurnRefused):
            legacy.confirm_turn("task-b", "turn-9", "", "")

    # stage_result: the six-step order and the parked rule

    def test_stage_result_follows_the_six_step_commit_order(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        events = []
        real_link, real_read = os.link, self.lc._read_strict

        def link(a, b, **k):
            events.append("link " + Path(b).name)
            return real_link(a, b, **k)

        def read(p):
            if Path(p).name.startswith("blob."):
                events.append("read " + Path(p).name)
            return real_read(p)
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.lc.os, "link", link))
            stack.enter_context(mock.patch.object(self.lc, "_read_strict", read))
            self.assertEqual(life.stage_result("task-a", b"the answer\n", "turn-1"), "staged")
        rec = life.claims.load("task-a")
        blob = rec["staged"]["blob"]
        self.assertEqual(events, ["fsync <temp>", "replace claim.json", "dir task-a", "fsync <temp>", "link " + blob,
                                  "dir task-a", "read " + blob, "fsync <temp>", "replace claim.json", "dir task-a"],
                         "descriptor, temp, link, directory, verify, phase")
        self.assertEqual((rec["phase"], rec["staged"]["identity"], rec["staged"]["length"]),
                         ("RESULT_STAGED", {"task_id": "task-a", "attempt": 1, "generation": GEN, "turn_id": "turn-1"}, 11))
        self.assertEqual(rec["staged"]["sha256"], self.lc._sha256(b"the answer\n"))
        d = self.work / "state" / "claims" / "task-a"
        self.assertEqual(sorted(os.listdir(d)), [blob, "claim.json"], "no temp outlives the call")
        self.assertEqual((d / blob).read_bytes(), b"the answer\n")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_bytes(), b"the answer\n")
        self.assertTrue((d / blob).exists(), "the committed body is immutable; compaction removes the claim")

    def test_same_different_corrupt_and_duplicate_candidates(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        self.assertEqual(life.stage_result("task-a", b"first\n", "turn-2"), "parked", "not the recorded turn")
        self.assertEqual(life.claims.load("task-a")["phase"], "TURN_CONFIRMED")
        self.assertEqual(life.stage_result("task-a", b"first\n", "turn-1"), "staged")
        d = self.work / "state" / "claims" / "task-a"
        blob = life.claims.load("task-a")["staged"]["blob"]
        self.assertEqual(life.stage_result("task-a", b"first\n", "turn-1"), "staged", "an identical retry finishes")
        self.assertEqual(life.stage_result("task-a", b"second\n", "turn-1"), "parked", "different bytes park")
        self.assertEqual((d / blob).read_bytes(), b"first\n", "with the original intact")
        self.assertEqual(life.claims.load("task-a")["phase"], "RESULT_STAGED")
        # a retry that finds a final with other bytes (a corrupt or foreign candidate) parks the claim
        self.setUp()
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        before = self.claim_bytes()
        with mock.patch.object(self.lc.Lifecycle, "_commit_blob", side_effect=Crash):
            with self.assertRaises(Crash):
                life.stage_result("task-a", b"first\n", "turn-1")
        rec = life.claims.load("task-a")
        self.assertEqual(rec["phase"], "TURN_CONFIRMED", "the descriptor is anchored first")
        (self.work / "state" / "claims" / "task-a" / rec["staged"]["blob"]).write_bytes(b"corrupt\n")
        self.assertEqual(life.stage_result("task-a", b"first\n", "turn-1"), "parked", "a corrupt final parks")
        self.assertEqual(life.claims.load("task-a")["phase"], "TURN_CONFIRMED")
        self.assertEqual((self.work / "state" / "claims" / "task-a" / rec["staged"]["blob"]).read_bytes(), b"corrupt\n")
        self.assertEqual(life.stage_result("task-a", b"second\n", "turn-1"), "parked", "a second candidate parks")
        self.assertNotEqual(self.claim_bytes(), before)
        self.assertEqual(life.claims.load("task-a")["staged"]["sha256"], self.lc._sha256(b"first\n"),
                         "the first descriptor stands")
        self.assertFalse((self.results / "task-a.txt").exists())
        self.assertEqual(life.publish_and_settle("task-a"), "parked")
        again = self.life(lineage=lambda pid, start: False)
        again.recover(generation=GEN, proof=False)
        self.assertEqual(again.claims.load("task-a")["phase"], "TURN_CONFIRMED", "a mismatched blob parks in recovery too")
        self.assertFalse((self.results / "task-a.txt").exists())

    def test_the_existing_final_path_fsyncs_the_directory_before_the_phase_and_parks_while_it_fails(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        d = self.work / "state" / "claims" / "task-a"
        real = self.lc.fsync_dir

        def crash_after_the_link(path):
            if any(n.startswith("blob.") for n in os.listdir(d)):
                raise Crash
            return real(path)
        with mock.patch.object(self.lc, "fsync_dir", crash_after_the_link):
            with self.assertRaises(Crash):
                life.stage_result("task-a", b"body\n", "turn-1")
        rec = life.claims.load("task-a")
        self.assertTrue((d / rec["staged"]["blob"]).exists(), "the final was linked, its directory not yet synced")
        self.assertEqual(rec["phase"], "TURN_CONFIRMED")

        def failing(path):
            if Path(path).name == "task-a":
                raise OSError(errno.EIO, "fsync failed")
            return real(path)
        with mock.patch.object(self.lc, "fsync_dir", failing):
            for _ in range(2):
                self.assertEqual(life.stage_result("task-a", b"body\n", "turn-1"), "parked")
                self.assertEqual(life.claims.load("task-a")["phase"], "TURN_CONFIRMED", "stays while the fsync fails")
                self.assertEqual(life.publish_and_settle("task-a"), "parked")
                self.assertFalse((self.results / "task-a.txt").exists(), "zero publication")
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            self.assertEqual(life.stage_result("task-a", b"body\n", "turn-1"), "staged")
        self.assertEqual(events[events.index("dir task-a"):], ["dir task-a", "fsync <temp>", "replace claim.json", "dir task-a"],
                         "the existing final's directory is fsynced before RESULT_STAGED is saved")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_bytes(), b"body\n")

    def kill_at(self, boundary):
        """Run stage_result to the named boundary and die there; returns the claim's phase at death."""
        self.setUp()
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        saves = []
        real_save, real_link, real_fsync, real_verified = (self.lc.ClaimStore.save, os.link, self.lc.fsync_dir,
                                                          self.lc.Lifecycle._verified)

        def save(store, rec):
            real_save(store, rec)
            saves.append(rec["phase"])
            if boundary == "descriptor" and rec["phase"] == "TURN_CONFIRMED" and rec.get("staged"):
                raise Crash
            if boundary == "phase" and rec["phase"] == "RESULT_STAGED":
                raise Crash

        def link(a, b, **k):
            if boundary == "temp":
                raise Crash
            real_link(a, b, **k)
            if boundary == "link":
                raise Crash

        def fsync(path):
            real_fsync(path)
            if boundary == "dirsync" and Path(path).name == "task-a" and saves == ["TURN_CONFIRMED"]:
                raise Crash

        def verified(lc, task_id, descriptor):
            out = real_verified(lc, task_id, descriptor)
            if boundary == "verify":
                raise Crash
            return out
        with mock.patch.object(self.lc.ClaimStore, "save", save), mock.patch.object(self.lc.os, "link", link), \
                mock.patch.object(self.lc, "fsync_dir", fsync), mock.patch.object(self.lc.Lifecycle, "_verified", verified):
            with self.assertRaises(Crash):
                life.stage_result("task-a", b"body\n", "turn-1")
        return life.claims.load("task-a")["phase"]

    def test_a_writer_killed_between_the_temp_fsync_and_the_link_then_a_same_bytes_retry_stages(self):
        """A real kill leaves the fsynced temp behind (no finally runs); the retry writes its own temp,
        links the final and stages; the dead writer's temp is swept, never taken for the final."""
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        d = self.work / "state" / "claims" / "task-a"
        pid = os.fork()
        if pid == 0:                                    # the writer, killed at the link
            try:
                self.lc.os.link = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
                life.stage_result("task-a", b"body\n", "turn-1")
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        left = [n for n in os.listdir(d) if n.endswith(".tmp")]
        self.assertEqual(len(left), 1, "the killed writer's fsynced temp is left")
        self.assertEqual((d / left[0]).read_bytes(), b"body\n")
        self.assertFalse(any(n.startswith("blob.") for n in os.listdir(d)), "no final was linked")
        rec = life.claims.load("task-a")
        self.assertEqual((rec["phase"], rec["staged"]["sha256"]), ("TURN_CONFIRMED", self.lc._sha256(b"body\n")))
        again = self.life(lineage=lambda p, start: False if p == pid else None)
        self.assertEqual(again.stage_result("task-a", b"body\n", "turn-1"), "staged", "the same bytes finish")
        self.assertEqual((d / rec["staged"]["blob"]).read_bytes(), b"body\n")
        self.assertEqual(again.stage_result("task-a", b"other\n", "turn-1"), "parked", "different bytes never")
        again._sweep_temps("task-a", time.time())
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [], "the dead writer's temp is swept")
        self.assertEqual(again.publish_and_settle("task-a"), "settled")
        self.assertEqual((self.results / "task-a.txt").read_bytes(), b"body\n")

    def test_a_kill_at_each_of_the_six_boundaries_is_recovered_twice(self):
        for boundary, expect in (("descriptor", b"interrupted\n"), ("temp", b"interrupted\n"), ("link", b"body\n"),
                                 ("dirsync", b"body\n"), ("verify", b"body\n"), ("phase", b"body\n")):
            with self.subTest(boundary=boundary):
                phase = self.kill_at(boundary)
                self.assertEqual(phase, "RESULT_STAGED" if boundary == "phase" else "TURN_CONFIRMED")
                d = self.work / "state" / "claims" / "task-a"
                temps = [n for n in os.listdir(d) if n.endswith(".tmp")]
                self.assertEqual(temps, [], "a temp never outlives the call")
                again = self.life(lineage=lambda pid, start: False)
                self.assertEqual(again.recover(now=None), None)
                self.assertEqual(again.claims.load("task-a")["phase"], phase, "the cloud path leaves a local claim alone")
                again.recover(generation=OTHER, proof=False)
                self.assertEqual(again.claims.load("task-a")["phase"], phase, "another generation's path leaves it alone")
                again.recover(generation=GEN, proof=False)
                self.assertEqual((self.results / "task-a.txt").read_bytes(), expect)
                self.assertEqual(again.claims.load("task-a")["phase"], "SETTLED")
                self.assertIn("task-a", again.settled)
                before = snapshot(self.work)
                self.life(lineage=lambda pid, start: False).recover(generation=GEN, proof=False)
                self.assertEqual(snapshot(self.work), before, "the second recovery changes nothing")
                self.assertEqual([n for n in os.listdir(self.results) if n.startswith("task-a")], ["task-a.txt"],
                                 "published exactly once")

    def test_the_sweep_removes_only_a_dead_or_stale_temp_and_fsyncs_after_each(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        d = self.work / "state" / "claims" / "task-a"
        digest = "a" * 24
        dead = d / (".task-a.1.%s.%d.deadbeef.tmp" % (digest, 2 ** 22 - 1))
        live = d / (".task-a.1.%s.%d.cafebabe.tmp" % (digest, 4242))
        stale = d / (".task-a.1.%s.%d.0badf00d.tmp" % (digest, 4343))
        final = d / ("blob.1.%s" % digest)
        for p in (dead, live, stale, final):
            p.write_bytes(b"x")
        old = self.clock.t - self.lc.TEMP_SWEEP_AFTER - 1
        os.utime(stale, (old, old))
        os.utime(final, (old, old))
        os.utime(live, (self.clock.t, self.clock.t))        # young on the lifecycle's clock
        os.utime(dead, (self.clock.t, self.clock.t))        # young too: swept for its dead pid alone
        events = []
        lineage = lambda pid, start: False if pid == 2 ** 22 - 1 else None  # noqa: E731
        real_unlink = os.unlink
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.lc.os, "unlink",
                                                  lambda p, **k: (events.append("unlink " + Path(p).name), real_unlink(p, **k))[1]))
            self.life(lineage=lineage).recover(generation=GEN, proof=False)
        self.assertFalse(dead.exists(), "a dead writer's temp is swept")
        self.assertFalse(stale.exists(), "an over-age temp is swept")
        self.assertTrue(live.exists(), "a live (unobserved) writer's young temp is spared")
        self.assertTrue(final.exists(), "never a final")
        swept = [i for i, e in enumerate(events) if e in ("unlink " + dead.name, "unlink " + stale.name)]
        self.assertEqual(len(swept), 2, events)
        for i in swept:
            self.assertEqual(events[i + 1], "dir task-a", "each sweep unlink is followed by a directory fsync: %s" % events[i:i + 2])
        self.assertEqual(life.claims.load("task-a")["phase"], "SETTLED")

    # the parked rule and mode enforcement

    def test_publish_and_settle_is_parked_for_a_phase_it_did_not_advance(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        self.assertEqual(life.publish_and_settle("task-a"), "parked", "TURN_CONFIRMED")
        self.assertEqual(life.publish_and_settle("task-zz"), "parked", "no claim")
        rec = life.claims.load("task-a")
        life.claims.save(dict(rec, phase="SOMETHING_NEW"))
        self.assertEqual(life.publish_and_settle("task-a"), "parked", "an unknown phase")
        self.assertFalse((self.results / "task-a.txt").exists())
        life.claims.save(rec)
        self.assertEqual(life.stage_result("task-a", b"ok\n", "turn-1"), "staged")
        self.assertEqual(life.publish_and_settle("task-a"), "settled")
        self.assertEqual(life.publish_and_settle("task-a"), "settled", "already SETTLED stays settled")

    def test_legacy_recover_refuses_local_claims_and_the_generation_path_settles_them(self):
        self.local_dir()
        life = self.life()
        self.confirmed(life)
        self.confirmed(life, "task-b")
        self.assertEqual(life.stage_result("task-b", b"b\n", "turn-1"), "staged")
        self.task("task-c")
        n = life.prepare("task-c", "ic\n")
        life.begin_call("task-c", n, self.work / "tasks" / "task-c.txt")
        life.record_outcome("task-c", "queued", "[no-send]\n")
        life.claims.save({k: v for k, v in life.claims.load("task-c").items() if k not in ("claim_mode", "claim_version", "writer")})
        before = snapshot(self.work / "state")
        self.logs = []
        again = self.life(lineage=lambda pid, start: False)
        again.recover()
        after = snapshot(self.work / "state")
        self.assertEqual((self.results / "task-c.txt").read_bytes(), b"[no-send]\n", "the legacy claim is published")
        for tid in ("task-a", "task-b"):
            self.assertEqual(after["claims/%s/claim.json" % tid], before["claims/%s/claim.json" % tid], tid)
            self.assertFalse((self.results / ("%s.txt" % tid)).exists(), tid)
        self.assertEqual({k: v for k, v in after.items() if k.startswith("claims/task-b/blob")},
                         {k: v for k, v in before.items() if k.startswith("claims/task-b/blob")})
        self.assertEqual(sorted(self.logs.count("foreign claim (local), left alone -> %s" % t) for t in ("task-a", "task-b")), [1, 1])
        self.assertEqual(sorted(again.settled.ids), ["task-c"], "the ledger holds only the legacy claim")
        with self.assertRaises(ValueError):
            again.recover(generation=GEN)
        with self.assertRaises(ValueError):
            again.recover(generation=GEN, proof=True)
        self.assertEqual(snapshot(self.work / "state"), after, "no proof, no effect")
        again.recover(generation=GEN, proof=False)
        self.assertEqual((self.results / "task-a.txt").read_bytes(), b"interrupted\n", "turn confirmed, no result")
        self.assertEqual((self.results / "task-b.txt").read_bytes(), b"b\n", "the verified blob, exactly")
        self.assertEqual(again.claims.load("task-a")["outcome"]["detail"], "turn confirmed, no result")
        self.assertEqual(sorted(again.settled.ids), ["task-a", "task-b", "task-c"])
        self.assertEqual(list(again.journal.charges()), [("task-a", 1), ("task-b", 1), ("task-c", 1)])

    def test_the_state_allowlist_admits_the_local_records_and_locks_as_plain_files(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        for name in ("claude-local-plugin.json", "claude-local-supervisor.json", "claims.lock", "progress.lock"):
            self.assertIn(name, self.lc.STATE_ALLOWED)
            self.assertIn(name, self.lc.STATE_OPTIONAL_FILES)
            (self.work / "state" / name).write_text("")
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        (self.work / "state" / "claims.lock").unlink()
        (self.work / "state" / "claims.lock").mkdir()
        self.assertIn("not a plain file", self.lc.prepare_work_dir(str(self.work)))

    def test_unmodified_main_refuses_the_fenced_work_dir_at_startup_with_everything_unchanged(self):
        old = main_lifecycle()
        self.assertEqual(old.FORMAT, "cloud-session/1")
        self.assertFalse(hasattr(old, "FORMAT_LOCAL"))
        self.local_dir()
        life = self.life()
        self.claimed(life, "task-p")
        self.confirmed(life, "task-t")
        self.confirmed(life, "task-s")
        self.assertEqual(life.stage_result("task-s", b"s\n", "turn-1"), "staged")
        before = snapshot(self.work)
        refusal = old.prepare_work_dir(str(self.work))
        self.assertIsNotNone(refusal)
        self.assertIn("claude-local/1", refusal)
        self.assertEqual(snapshot(self.work), before, "claim, body, result and ledger untouched")
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "this binary serves it")
        fresh = Path(self.tmp.name + "-fresh")
        fresh.mkdir()
        self.addCleanup(shutil.rmtree, fresh, True)
        self.assertIsNone(old.prepare_work_dir(str(fresh)))
        with self.assertRaises(self.lc.StateError):
            self.lc.enable_local_format(str(Path(self.tmp.name + "-none")))
        self.assertIsNone(self.lc.prepare_work_dir(str(fresh)), "a cloud-session/1 dir is still served")

    def test_the_fence_order_is_lock_then_state_then_marker(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        events = []
        with contextlib.ExitStack() as stack:
            for patch in durable_events(self.lc, events):
                stack.enter_context(patch)
            self.lc.enable_local_format(str(self.work))
        self.assertEqual(events, ["fsync claims.lock", "dir state", "fsync <temp>", "replace format", "dir state"])
        self.lc.enable_local_format(str(self.work))
        self.assertEqual((self.work / "state" / "format").read_text(), "claude-local/1\n", "idempotent")


class CloudStepRefusalTest(SpyMixin, unittest.TestCase):
    """The activated cloud adapter's step on a local TURN_CONFIRMED claim changes nothing; a cloud and
    an untagged-legacy RESULT_STAGED claim are the positive controls."""

    def adapter(self, work, claude):
        argv = ["--work", str(work), "--session-id", "session_X", "--claude", str(claude), "--poll", "0.01",
                "--timeout", "10", "--idle-exit", "0.1"]
        args = sa.build_parser().parse_args(argv)
        args.inbox, args.results, args.state = (os.path.join(str(work), d) for d in ("tasks", "results", "state"))
        return sa.Adapter(args, environ={"PATH": "/usr/bin:/bin", "HOME": str(work.parent)})

    def test_step_leaves_a_local_claim_alone_and_publishes_cloud_and_legacy_staged_claims(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            claude = self.fake_claude(d)
            self.assertIsNone(vendored.prepare_work_dir(str(work)))
            vendored.enable_local_format(str(work))
            life = vendored.Lifecycle(work, clock=Clock(), log=quiet)
            for tid in ("task-l", "task-c", "task-g"):
                self.write_task(work, tid)
            n = life.prepare("task-l", "i\n", mode=vendored.LOCAL, generation=GEN)
            h = vendored._sha256(b"x")
            life.confirm_turn("task-l", "turn-1", life.authorize_call("task-l", n, text_hash=h), h)
            self.assertEqual(life.stage_result("task-l", b"local\n", "turn-1"), "staged")
            life.claims.save(dict(life.claims.load("task-l"), phase="TURN_CONFIRMED"))
            for tid in ("task-c", "task-g"):
                n = life.prepare(tid, "i\n")
                life.begin_call(tid, n, work / "tasks" / (tid + ".txt"))
                life.record_outcome(tid, "queued", tid.encode().decode() + "\n")
            life.claims.save({k: v for k, v in life.claims.load("task-g").items()
                              if k not in ("claim_mode", "claim_version", "writer")})
            self.assertNotIn("claim_mode", life.claims.load("task-g"))
            before = snapshot(work)
            err = io.StringIO()
            with self.write_spy(work) as seen, contextlib.redirect_stderr(err):
                ad = self.adapter(work, claude)
                done = set()
                self.assertFalse(ad.step("task-l.txt", done))
                self.assertEqual(done, set(), "no seen set")
                self.assertEqual(snapshot(work), before, "no ledger, body or result changed")
                self.assertIn("foreign claim (local)", err.getvalue())
                self.assertTrue(ad.step("task-c.txt", done))
                self.assertTrue(ad.step("task-g.txt", done))
            self.assertEqual({p for who, p in seen if "task-l" in p}, set(), "not one write touched the local claim")
            self.assertEqual((work / "results" / "task-c.txt").read_text(), "task-c\n")
            self.assertEqual((work / "results" / "task-g.txt").read_text(), "task-g\n")
            self.assertFalse((work / "results" / "task-l.txt").exists())
            self.assertEqual(done, {"task-c", "task-g"})
            self.assertEqual((Path(d) / "bin" / "claude.calls").exists(), False, "no provider call")


class BridgeEntryTest(Base):
    """The gateway bridge's own entries in a work root its state dir sits in, run against both copies."""

    def bridge_writes(self, work):
        """What the gateway bridge writes when its state dir sits directly in the work root, through its
        own writers: the poller lock (workspace_lock), the default HITL store, and its log."""
        from ag2_sparrow import workspace_lock
        from ag2_sparrow.hitl.manager import HitlStore, default_store
        src = (PKG_ROOT / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
        for pin in ('_LOG_DIR = _STATE.parent / "logs"', '_LOG_FILE = _LOG_DIR / "gateway-bridge.log"',
                    '_LOCK_ROLE = f"gateway-bridge{_INST_SUFFIX}"', "_LOCK_WS = _STATE.parent",
                    "manager = HitlManager(HitlStore(default_store(workspace)))", '_LOG_FILE.with_suffix(".log.1")'):
            self.assertIn(pin, src, "the bridge's layout moved: update task_lifecycle's BRIDGE_* names")
        self.assertEqual(workspace_lock.acquire("gateway-bridge", Path(work)).status, "acquired")
        workspace_lock.acquire("gateway-bridge.second", Path(work))
        HitlStore(default_store(Path(work)))
        (Path(work) / "logs").mkdir(exist_ok=True)
        for name in ("gateway-bridge.log", "gateway-bridge.log.1"):
            (Path(work) / "logs" / name).write_text("[remote-gateway-bridge] line\n")

    def test_a_root_the_bridge_used_still_allows_a_new_session(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "the adapter initialises the root")
        self.bridge_writes(self.work)
        self.assertTrue((self.work / "state" / "locks" / "gateway-bridge.lock.guard").is_file())
        self.assertTrue((self.work / "state" / "hitl" / "requirements").is_dir())
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)), "the old session gone, a new one may start")
        with tempfile.TemporaryDirectory() as d:
            self.bridge_writes(d)
            self.assertIsNone(self.lc.prepare_work_dir(d, check_only=True))
            self.assertFalse(os.path.exists(os.path.join(d, "state", "format")), "check_only writes nothing")
            self.assertIsNone(self.lc.prepare_work_dir(d), "the bridge ran first: still a fresh root")
        foreign = {"a foreign lock": ("state/locks/other.lock", "file"), "a foreign store": ("state/hitl/notes", "dir"),
                   "a foreign log": ("logs/other.log", "file"), "a link for the lock": ("state/locks/gateway-bridge.lock", "link")}
        for what, (rel, kind) in foreign.items():
            for marked in (True, False) if not rel.startswith("logs/") else (False,):
                with self.subTest(what=what, marked=marked), tempfile.TemporaryDirectory() as d:
                    if marked:
                        self.assertIsNone(self.lc.prepare_work_dir(d))
                    path = Path(d) / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    {"file": lambda: path.write_text("x"), "dir": path.mkdir,
                     "link": lambda: os.symlink(Path(d) / "elsewhere", path)}[kind]()
                    line = self.lc.prepare_work_dir(d)
                    self.assertIsNotNone(line, rel)
                    self.assertIn(rel.split("/")[0], line)

    def test_check_only_leaves_a_stale_record_temp_that_a_start_sweeps(self):
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        temp = self.work / "state" / ".settled-ids.1.abcdef.tmp"
        temp.write_text("x")
        old = time.time() - self.lc.TEMP_SWEEP_AFTER - 5
        os.utime(temp, (old, old))
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work), check_only=True))
        self.assertTrue(temp.exists(), "a check writes nothing")
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work)))
        self.assertFalse(temp.exists())

    def test_a_layout_refusal_and_an_unreadable_root_read_differently(self):
        (self.work / "stray").write_text("x")
        self.assertFalse(self.lc.prepare_work_dir(str(self.work)).startswith(self.lc.UNCHECKED))
        with mock.patch.object(self.lc.os, "listdir", side_effect=PermissionError(13, "denied")):
            self.assertTrue(self.lc.prepare_work_dir(str(self.work)).startswith(self.lc.UNCHECKED))
        self.assertIsNone(self.lc.prepare_work_dir(str(self.work / "absent"), check_only=True), "install creates it")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    copies = [("vendored", vendored)]
    src = src_copy()
    if src is not None:
        copies.append(("src", src))
    for label, mod in copies:
        for case in (LockTest, FreshDirTest, ProbeTest, ProbeBothDirsTest, ClassifyTest, PublicationTest,
                     JournalTest, SettledIdsTest, DecisionTest, ErrorBranchTest, LocalClaimTest, BridgeEntryTest):
            if label == "src" and case in (LockTest, FreshDirTest, ProbeBothDirsTest):
                continue
            bound = GATE(type("%s_%s" % (case.__name__, label), (case,), {"lc": mod}))
            suite.addTests(loader.loadTestsFromTestCase(bound))
    suite.addTests(loader.loadTestsFromTestCase(GATE(StructureTest)))
    suite.addTests(loader.loadTestsFromTestCase(GATE(CloudStepRefusalTest)))
    suite.addTests(loader.loadTestsFromTestCase(BoundaryGuardTest))
    return suite


if __name__ == "__main__":
    unittest.main()
