#!/usr/bin/env python3
"""The holder_lock contract (docs/design-holder-lock.md): every HL case runs against src/holder_lock.py
and against the vendored ag2_sparrow/holder_lock.py. In-process cases point the module's home at a temp
dir; the cases that start real processes use the real home of this uid and remove their lock dir.

Run: python3 tests/holder-lock.test.py
"""
import ast
import contextlib
import copy
import dataclasses
import stat
import io
import json
import os
import pickle
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import pwd
except ImportError:
    pwd = None

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import holder_lock as SRC  # noqa: E402

from ag2_sparrow import holder_lock as VENDORED  # noqa: E402

from holder_lock_contract import FirstActContract, snapshot  # noqa: E402

POSIX = os.name == "posix"
WAIT = 10.0
BOOT = """
import sys
kind, home = sys.argv[1], sys.argv[2]
if kind == "src":
    sys.path.insert(0, %r)
    import holder_lock as hl
else:
    sys.path.insert(0, %r)
    from ag2_sparrow import holder_lock as hl
if home != "-":
    hl._home = lambda: home
exec(sys.argv[3])
""" % (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow"))
PKG_READMES = [REPO / "packages" / "ag2-sparrow" / "README.md",
               REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "adapters" / "claude_routine" / "README.md",
               REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "adapters" / "codex" / "README.md"]


def read_line(stream, timeout=WAIT, default=AssertionError):
    """One line from a child's pipe, read with os.read on the raw fd into a buffer kept on the stream
    object (never keyed by fd number, which a later pipe may reuse), so two lines arriving in one read
    are both kept. After `timeout`: raise, or return `default`."""
    fd = stream.fileno()
    buf = getattr(stream, "_hl_pending", b"")
    deadline = time.monotonic() + timeout
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            stream._hl_pending = buf
            if default is AssertionError:
                raise AssertionError("no line within %.0fs" % timeout)
            return default
        chunk = os.read(fd, 4096)
        if not chunk:
            stream._hl_pending = b""
            return buf.decode().strip()
        buf += chunk
    line, _, stream._hl_pending = buf.partition(b"\n")
    return line.decode().strip()


@unittest.skipUnless(POSIX, "holder_lock is POSIX-only; the Windows job runs only the platform case")
class HolderLockTest(unittest.TestCase):
    hl = SRC
    kind = "src"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.home_patch = mock.patch.object(self.hl, "_home", lambda: str(self.home))
        self.home_patch.start()
        self.home_patched = True
        self.addCleanup(self.real_home)
        self.root = self.tmp / "work"
        self.addCleanup(self.drop_owner)

    def real_home(self):
        """From here on the module resolves this uid's real home, as a child process would."""
        if self.home_patched:
            self.home_patch.stop()
            self.home_patched = False

    def drop_owner(self):
        owner = self.hl._OWNER
        if owner is not None and owner.pid == os.getpid() and not owner.released:
            with contextlib.suppress(Exception):
                self.hl.release(owner)
        self.hl._OWNER = None
        self.hl._WAITING = None

    # ── helpers ──

    def proc(self):
        """A live process and its start time."""
        p = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (p.kill(), p.wait(WAIT)))
        self.procs = dict(getattr(self, "procs", {}), **{str(p.pid): p})
        start = None
        for _ in range(50):
            start = self.hl.start_time(p.pid)
            if start:
                break
            time.sleep(0.05)
        return p.pid, start

    def end(self, pid):
        p = self.procs[str(pid)]
        p.kill()
        p.wait(WAIT)

    @staticmethod
    def kill_quiet(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def exit_owner(self, owner):
        """What the owner's process exit does to a handle release refused: its descriptors close."""
        for fd in (owner.fd, owner.dir_fd, owner.root_fd):
            os.close(fd)
        owner.released = True
        self.hl._OWNER = None

    def dead(self):
        """A pid that ran and is gone, with the start it had."""
        p = subprocess.Popen(["sleep", "30"])
        start = self.hl.start_time(p.pid)
        p.kill()
        p.wait(WAIT)
        return p.pid, start

    def record(self, pid, start, **over):
        rec = {"schema": 1, "adapter": "routine", "pid": pid, "start": start, "host": self.hl._host(),
               "install_id": self.hl.install_id(), "lock": {"dev": 1, "ino": 2}, "acquired_at": 1.5}
        rec.update(over)
        return rec

    def record2(self, pid, start, attached=None, **over):
        """A schema-2 record whose holder is (pid, start); `attached` = (pid, start) attaches a consumer
        and moves the fencing pid to it, as attach_consumer does."""
        rec = dict(self.record(pid, start), schema=2, owner_pid=pid, owner_start=start, consumer=None, tombstone=None)
        if attached is not None:
            rec.update(pid=attached[0], start=attached[1], consumer={"pid": attached[0], "start": attached[1]})
        rec.update(over)
        return rec

    def main_module(self):
        """The schema-1 holder_lock checked in as a fixture, loaded as its own module with this test's
        home. A missing fixture is an error, never a skip."""
        import importlib.util
        path = REPO / "tests" / "fixtures" / "old_main" / "holder_lock_schema1.py"
        spec = importlib.util.spec_from_file_location("holder_lock_main_%s" % self.kind, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod                      # dataclasses resolve the module by name
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(mod)
        mod._home = lambda: str(self.home)
        self.addCleanup(lambda: setattr(mod, "_OWNER", None))
        return mod

    def ldir(self, root=None):
        """The lock dir of `root`, which is created if absent (the key is the directory's inode)."""
        root = Path(root or self.root)
        root.mkdir(parents=True, exist_ok=True)
        return Path(self.hl.lock_dir(str(root)))

    def forget_lock_dir(self, root):
        """At cleanup, remove the real-home lock dir of `root` if the root still exists."""
        def drop():
            if os.path.isdir(root):
                shutil.rmtree(self.hl.lock_dir(root), True)
        self.addCleanup(drop)

    def put(self, rec, root=None):
        d = self.ldir(root)
        d.mkdir(parents=True, exist_ok=True)
        data = rec if isinstance(rec, bytes) else json.dumps(rec).encode()
        (d / "holder").write_bytes(data)
        return data

    @contextlib.contextmanager
    def flock_elsewhere(self, root=None):
        """The lock held through another open file description, as another process would."""
        d = self.ldir(root)
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertTrue(self.hl.file_lock.try_lock_fd(fd))
            yield
        finally:
            os.close(fd)

    def acquire(self, root=None, **kw):
        return self.hl.acquire(str(root or self.root), install_id=self.hl.install_id(), **kw)

    def owned(self, root=None):
        r = self.acquire(root)
        self.assertIsInstance(r, self.hl.Acquired, r)
        return r.owner

    def lock_free(self, root=None, directory=None):
        fd = os.open(str(Path(directory or self.ldir(root)) / "lock"), os.O_RDWR)
        try:
            free = self.hl.file_lock.try_lock_fd(fd)
        finally:
            os.close(fd)
        return free

    def run_py(self, code, home=None, env=None, **kw):
        return subprocess.Popen([sys.executable, "-c", BOOT, self.kind, home or str(self.home), code],
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, **kw)

    # ── §1.1 acquire ──

    def test_acquire_publishes_nothing_and_returns_the_prior_under_the_flock(self):
        pid, start = self.dead()
        data = self.put(self.record(pid, start))
        order = []
        real_lock, real_read = self.hl.file_lock.try_lock_fd, self.hl._read_prior
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", lambda fd: (order.append("flock"), real_lock(fd))[1]), \
                mock.patch.object(self.hl, "_read_prior", lambda d: (order.append("read"), real_read(d))[1]):
            r = self.acquire()
        self.assertIsInstance(r, self.hl.Acquired)
        self.assertEqual(r.prior, self.hl.Valid(json.loads(data)))
        self.assertEqual(order, ["flock", "read"], "the prior is read under the flock")
        self.assertEqual((self.ldir() / "holder").read_bytes(), data, "nothing is published by acquire")
        self.assertEqual(sorted(os.listdir(self.ldir())), ["holder", "lock"])
        self.assertTrue(r.owner.lock_id and r.owner.pid == os.getpid() and r.owner.start)
        self.assertTrue(os.get_inheritable(r.owner.fd) is False, "FD_CLOEXEC")

    def test_a_live_or_unobserved_holder_is_held_and_a_dead_one_is_overwritten(self):
        pid, start = self.proc()
        self.put(self.record(pid, start))
        r = self.acquire()
        self.assertEqual((type(r).__name__, r.reason), ("Held", "live holder"))
        self.put(self.record(pid, start, host="another-host"))
        self.assertEqual(self.acquire().reason, "unobserved")
        dpid, dstart = self.dead()
        self.put(self.record(dpid, dstart))
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.assertEqual(self.hl.read_holder(str(self.root)).record["pid"], os.getpid(), "overwritten on publish")

    def test_an_invalid_record_refuses_on_a_live_or_unobserved_pid_and_is_overwritten_otherwise(self):
        pid, start = self.proc()
        rec = self.record(pid, start)
        del rec["adapter"]
        for raw in (json.dumps(rec).encode(), b'{"pid": %d, "start": ' % pid, json.dumps(dict(rec, start=None)).encode()):
            with self.subTest(raw=raw[:40]):
                self.put(raw)
                r = self.acquire()
                self.assertIsInstance(r, self.hl.Held)
                self.assertEqual(r.reason, "invalid record naming a live or unobserved pid")
        dpid, _ = self.dead()
        self.put(b'{"pid": %d, "torn' % dpid)
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Valid)

    def test_a_loser_gets_contended_with_the_current_record_or_missing_while_the_winner_is_pre_publish(self):
        with self.flock_elsewhere():
            r = self.acquire()
            self.assertEqual(r, self.hl.Contended(self.hl.Missing()), "the winner has not published yet")
            pid, start = self.proc()
            data = self.put(self.record(pid, start))
            self.assertEqual(self.acquire(), self.hl.Contended(self.hl.Valid(json.loads(data))))

    def test_a_replaced_lock_file_or_a_changed_root_is_refused(self):
        self.root.mkdir()
        real = self.hl.file_lock.try_lock_fd
        lock = self.ldir() / "lock"

        def replace_then_lock(fd):
            (lock.parent / "fresh").write_text("")
            os.replace(lock.parent / "fresh", lock)
            return real(fd)
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", replace_then_lock):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "replaced")
        self.assertTrue(self.lock_free(), "unlocked on the refusal")

        def swap_then_lock(fd):
            os.rename(self.root, self.tmp / "moved")
            os.symlink(self.tmp / "moved", self.root)
            return real(fd)
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", swap_then_lock):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "root changed")
        self.assertIsNone(self.hl._OWNER)

    def test_two_concurrent_acquires_in_one_process_let_exactly_one_proceed(self):
        inside, go = threading.Event(), threading.Event()
        real_statfs, real_open = self.hl._statfs_type, os.open
        other = self.tmp / "other"
        other.mkdir()
        other_lock = str(self.ldir(other) / "lock")
        opened = []

        def statfs(path):
            if not inside.is_set():
                inside.set()
                go.wait(10)
            return real_statfs(path)

        def spy_open(path, *a, **k):
            opened.append(os.fspath(path))
            return real_open(path, *a, **k)
        results = {}

        def run(name, root):
            try:
                results[name] = type(self.acquire(root)).__name__
            except self.hl.LockError as e:
                results[name] = "LockError: %s" % e
        with mock.patch.object(self.hl, "_statfs_type", statfs), mock.patch.object(self.hl.os, "open", spy_open):
            first = threading.Thread(target=run, args=("first", self.root))
            first.start()
            self.assertTrue(inside.wait(10), "the first acquire is past its owner check")
            second = threading.Thread(target=run, args=("second", other))
            second.start()
            second.join(0.5)
            go.set()
            first.join(10)
            second.join(10)
        self.assertEqual(results, {"first": "Acquired", "second": "LockError: already owner"})
        self.assertNotIn(other_lock, opened, "the second never touched its lock")

    def test_one_owner_per_process(self):
        owner = self.owned()
        with self.assertRaises(self.hl.LockError) as e:
            self.acquire(self.tmp / "other")
        self.assertEqual(str(e.exception), "already owner")
        self.hl.release(owner)
        self.hl.release(self.owned(self.tmp / "other"))

    # ── §1.2 wait ──

    def test_each_waiter_owns_its_own_file_and_a_newer_waiter_exiting_first_leaves_the_older_visible(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        code = ("import os, sys, time\nprint(os.getpid(), flush=True)\n"
                "r = hl.wait(%r, install_id=hl.install_id(), deadline=time.monotonic() + float(sys.argv[4]),"
                " poll=0.05)\nprint(type(r).__name__, flush=True)\n" % str(self.root))
        older = subprocess.Popen([sys.executable, "-c", BOOT, self.kind, str(self.home), code, "6"],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (older.kill(), older.wait(WAIT)))
        older_pid = int(read_line(older.stdout))
        newer = subprocess.Popen([sys.executable, "-c", BOOT, self.kind, str(self.home), code, "1"],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (newer.kill(), newer.wait(WAIT)))
        newer_pid = int(read_line(newer.stdout))
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not {"waiting.%d" % older_pid, "waiting.%d" % newer_pid} <= set(os.listdir(self.ldir())):
            time.sleep(0.05)
        self.assertEqual(read_line(newer.stdout), "TimedOut")
        newer.wait(WAIT)
        names = set(os.listdir(self.ldir()))
        self.assertNotIn("waiting.%d" % newer_pid, names, "the newer removed its own file")
        self.assertIn("waiting.%d" % older_pid, names, "and left the older's in place")
        self.assertEqual(self.hl.status(str(self.root))["waiters"], [older_pid])
        older.kill()
        older.wait(WAIT)

    def test_same_process_overlapping_waits_are_refused(self):
        class Stop(Exception):
            pass
        seen = []

        def tick(elapsed, holder):
            try:
                self.hl.wait(str(self.root), install_id="x", deadline=time.monotonic() + 1)
            except self.hl.LockError as e:
                seen.append(str(e))
            raise Stop()
        with self.flock_elsewhere(), self.assertRaises(Stop):
            self.hl.wait(str(self.root), install_id="x", deadline=time.monotonic() + 5, poll=0.05, on_tick=tick)
        self.assertEqual(seen, ["already waiting"])
        self.assertEqual([n for n in os.listdir(self.ldir()) if n.startswith("waiting.")], [])

    def test_wait_calls_on_tick_and_honours_an_absolute_deadline(self):
        ticks = []
        with self.flock_elsewhere():
            began = time.monotonic()
            r = self.hl.wait(str(self.root), install_id="x", deadline=began + 0.5, poll=0.1,
                             on_tick=lambda e, h: ticks.append((e, h)))
            took = time.monotonic() - began
        self.assertIsInstance(r, self.hl.TimedOut)
        self.assertGreaterEqual(len(ticks), 3)
        self.assertEqual([e for e, _ in ticks], sorted(e for e, _ in ticks))
        self.assertTrue(0.5 <= took < 2.0, took)
        self.assertEqual(ticks[0][1], self.hl.Missing())

    # ── §1.3 publish ──

    def test_publish_is_once_per_owner_and_fills_every_field(self):
        owner = self.owned()
        rec = self.hl.publish(owner, "routine").record
        self.assertEqual(sorted(rec), sorted(self.hl.RECORD_FIELDS))
        self.assertEqual((rec["schema"], rec["adapter"], rec["pid"], rec["start"], rec["host"], rec["install_id"]),
                         (self.hl.SCHEMA, "routine", os.getpid(), self.hl.start_time(os.getpid()), self.hl._host(),
                          self.hl.install_id()))
        self.assertEqual((rec["lock"]["dev"], rec["lock"]["ino"]), owner.lock_id)
        self.assertIsInstance(rec["acquired_at"], float)
        self.assertEqual((rec["owner_pid"], rec["owner_start"], rec["consumer"], rec["tombstone"]),
                         (os.getpid(), owner.start, None, None), "schema 2: the owner fences until a consumer attaches")
        self.assertEqual(self.hl.read_holder(str(self.root)), self.hl.Valid(rec))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "published")

    def test_publish_overwrites_any_prior_and_leaves_no_temp(self):
        pid, start = self.dead()
        self.put(self.record(pid, start))
        (self.ldir() / ".holder.1.abcdef12.tmp").write_text("a crash's temp")
        owner = self.owned()
        self.hl.publish(owner, "codex")
        self.assertEqual(self.hl.read_holder(str(self.root)).record["adapter"], "codex")
        self.assertEqual(sorted(os.listdir(self.ldir())), ["holder", "lock"])

    def test_publish_refuses_a_released_owner_a_closed_fd_or_a_replaced_file(self):
        owner = self.owned()
        self.hl.release(owner)
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "released")
        owner = self.owned()
        os.close(owner.fd)
        with self.assertRaises(self.hl.LockError):
            self.hl.publish(owner, "routine")
        owner.released, self.hl._OWNER = True, None
        owner = self.owned()
        (self.ldir() / "fresh").write_text("")
        os.replace(self.ldir() / "fresh", self.ldir() / "lock")
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "replaced")
        self.assertFalse((self.ldir() / "holder").exists(), "nothing written")

    # ── §1.4 release ──

    def test_release_removes_only_its_own_record_then_unlocks_and_is_single_use(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.assertEqual(self.hl.release(owner), self.hl.Released(True))
        self.assertFalse((self.ldir() / "holder").exists())
        self.assertTrue(self.lock_free())
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.release(owner)
        self.assertEqual(str(e.exception), "released")
        owner = self.owned()
        self.hl.publish(owner, "routine")
        pid, start = self.proc()
        theirs = self.put(self.record(pid, start))
        self.assertEqual(self.hl.release(owner), self.hl.Released(False))
        self.assertEqual((self.ldir() / "holder").read_bytes(), theirs, "a record naming someone else is left")
        self.assertTrue(self.lock_free())

    def test_release_from_a_forked_child_is_refused_and_the_parents_lock_stays(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        record = (self.ldir() / "holder").read_bytes()
        read, write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read)
            try:
                self.hl.release(owner)
                msg = b"released"
            except self.hl.LockError as e:
                msg = str(e).encode()
            except BaseException as e:
                msg = repr(e).encode()
            os.write(write, msg)
            os._exit(0)
        os.close(write)
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and os.waitpid(child, os.WNOHANG) == (0, 0):
            time.sleep(0.02)
        self.assertEqual(os.read(read, 200), b"not the owner")
        os.close(read)
        self.assertFalse(self.lock_free(), "the parent's lock stays")
        self.assertEqual((self.ldir() / "holder").read_bytes(), record)
        self.hl.release(owner)

    def test_nothing_in_the_module_unlinks_or_replaces_the_lock_file(self):
        source = Path(self.hl.__file__).read_text()
        self.assertNotRegex(source, r"\bftruncate\b|\btruncate\(")
        touched = []

        def spy(name):
            real = getattr(os, name)
            return lambda *a, **k: (touched.append((name, [os.fspath(x) for x in a[:2] if isinstance(x, (str, os.PathLike))])), real(*a, **k))[1]
        lock = str(self.ldir() / "lock")
        with contextlib.ExitStack() as stack:
            for name in ("unlink", "remove", "rename", "replace", "rmdir"):
                stack.enter_context(mock.patch.object(self.hl.os, name, spy(name)))
            owner = self.owned()
            ino = os.stat(lock).st_ino
            self.hl.publish(owner, "routine")
            self.hl.release(owner)
            with self.flock_elsewhere():
                self.acquire()
            self.hl.status(str(self.root))
        self.assertFalse([t for t in touched if lock in t[1]], touched)
        self.assertEqual(os.stat(lock).st_ino, ino)

    # ── §1.5, §1.6 reads and identity ──

    def test_read_holder_never_raises_and_classifies_every_malformed_shape(self):
        self.assertEqual(self.hl.read_holder(str(self.root)), self.hl.Missing())
        good = self.record(5, "2026-01-01T00:00:00Z")
        shapes = {
            "torn": b'{"schema": 1, "pid":',
            "non-object": b"[1, 2]",
            "not utf-8": b"\xff\xfe",
            "schema 2": json.dumps(dict(good, schema=2)).encode(),
            "missing field": json.dumps({k: v for k, v in good.items() if k != "host"}).encode(),
            "unknown field": json.dumps(dict(good, children=[])).encode(),
            "bool pid": json.dumps(dict(good, pid=True)).encode(),
            "zero pid": json.dumps(dict(good, pid=0)).encode(),
            "empty adapter": json.dumps(dict(good, adapter="")).encode(),
            "long host": json.dumps(dict(good, host="h" * 513)).encode(),
            "int acquired_at": json.dumps(dict(good, acquired_at=2)).encode(),
            "nan acquired_at": json.dumps(good).replace("1.5", "NaN").encode(),
            "lock shape": json.dumps(dict(good, lock={"dev": 1})).encode(),
        }
        for name, raw in shapes.items():
            with self.subTest(shape=name):
                self.put(raw)
                got = self.hl.read_holder(str(self.root))
                self.assertIsInstance(got, self.hl.Invalid)
                self.assertEqual(got.raw_bytes, raw)
        os.unlink(self.ldir() / "holder")
        os.mkdir(self.ldir() / "holder")
        self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Invalid, "unreadable, not raised")
        os.rmdir(self.ldir() / "holder")
        self.put(good)
        self.assertEqual(self.hl.read_holder(str(self.root)), self.hl.Valid(good))

    def test_same_file_rejects_a_replaced_or_missing_path(self):
        p = self.tmp / "f"
        p.write_text("")
        fd = os.open(str(p), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertTrue(self.hl.same_file(fd, p))
        os.symlink(p, self.tmp / "link")
        self.assertFalse(self.hl.same_file(fd, self.tmp / "link"), "a link is not followed")
        (self.tmp / "g").write_text("")
        os.replace(self.tmp / "g", p)
        self.assertFalse(self.hl.same_file(fd, p))
        os.unlink(p)
        self.assertFalse(self.hl.same_file(fd, p))

    def test_verify_lineage_is_tri_state_and_rejects_a_recycled_pid(self):
        me, start = os.getpid(), self.hl.start_time(os.getpid())
        self.assertIs(self.hl.verify_lineage(me, start), True)
        self.assertIs(self.hl.verify_lineage(me, start, self.hl._host()), True)
        self.assertIs(self.hl.verify_lineage(me, "2001-01-01T00:00:00Z"), False, "a recycled pid")
        pid, dstart = self.dead()
        self.assertIs(self.hl.verify_lineage(pid, dstart), False)
        self.assertIsNone(self.hl.verify_lineage(me, start, "another-host"))
        self.assertIsNone(self.hl.verify_lineage(me, None))
        with mock.patch.object(self.hl, "start_time", return_value=None):
            self.assertIsNone(self.hl.verify_lineage(me, start))

    def test_start_time_ignores_locale_and_time_zone(self):
        base = self.hl.start_time(os.getpid())
        self.assertRegex(base, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        with mock.patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8", "LANG": "de_DE.UTF-8", "TZ": "Asia/Tokyo"}):
            time.tzset()
            try:
                self.assertEqual(self.hl.start_time(os.getpid()), base)
            finally:
                pass
        time.tzset()
        self.assertIsNone(self.hl.start_time(2 ** 22 + 12345))

    def test_status_is_read_only_and_reports_an_invalid_record_stale_waiters_and_a_legacy_pidfile(self):
        self.root.mkdir()
        self.put(b'{"torn')
        pid, start = self.dead()
        (self.ldir() / ("waiting.%d" % pid)).write_text("%s 1.0\n" % start)
        live, _ = self.proc()
        (self.root / "state").mkdir()
        (self.root / "state" / "connector.pid").write_text("%d\n" % live)
        with self.flock_elsewhere():
            before = (snapshot(str(self.root)), snapshot(str(self.ldir())))
            st = self.hl.status(str(self.root))
            self.assertEqual((snapshot(str(self.root)), snapshot(str(self.ldir()))), before, "read-only")
        self.assertEqual(st["record"]["kind"], "invalid")
        self.assertEqual(st["stale_waiters"], [pid])
        self.assertEqual(st["legacy"]["pid"], live)

    # ── §1.7 platform ──

    def test_holder_lock_locks_only_through_file_lock(self):
        tree = ast.parse(Path(self.hl.__file__).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(imported & {"fcntl", "msvcrt"}, imported)
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attrs & {"flock", "lockf", "locking", "locked_file"})
        calls = []
        real = self.hl.file_lock.try_lock_fd
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", lambda fd: (calls.append(fd), real(fd))[1]):
            owner = self.owned()
        self.assertEqual(calls, [owner.fd])

    def test_on_windows_every_entry_point_raises_lock_error(self):
        dummy = self.hl.Owner(fd=-1, lock_id=(0, 0), pid=os.getpid(), start="s", root="r", directory="d",
                              install_id="i", acquired_at=1.0)
        entry = {"acquire": lambda: self.acquire(),
                 "wait": lambda: self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 1),
                 "publish": lambda: self.hl.publish(dummy, "routine"),
                 "release": lambda: self.hl.release(dummy),
                 "status": lambda: self.hl.status(str(self.root))}
        with mock.patch.object(self.hl.file_lock, "fcntl", None):
            for name, call in entry.items():
                with self.subTest(entry=name):
                    with self.assertRaises(self.hl.LockError) as e:
                        call()
                    self.assertEqual(str(e.exception), "unsupported platform: holder_lock is POSIX-only")
        self.real_home()
        with mock.patch.object(self.hl, "pwd", None):
            for name, call in {"read_holder": lambda: self.hl.read_holder(str(self.tmp)),
                               "lock_dir": lambda: self.hl.lock_dir(str(self.tmp))}.items():
                with self.subTest(helper=name), self.assertRaises(self.hl.LockError):
                    call()
        self.assertFalse(self.root.exists(), "nothing created")

    # ── §2 the record ──

    def test_record_field_types_and_bounds_are_enforced_on_read_and_publish(self):
        good = self.record(5, "s")
        for bad in (dict(good, pid=True), dict(good, start="s" * 513), dict(good, extra={}),
                    dict(good, lock={"dev": 1.0, "ino": 2})):
            self.put(bad)
            self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Invalid, bad)
        os.unlink(self.ldir() / "holder")
        owner = self.owned()
        for adapter in ("", "a" * 513, 5, None):
            with self.subTest(adapter=adapter):
                with self.assertRaises(self.hl.LockError):
                    self.hl.publish(owner, adapter)
                self.assertFalse((self.ldir() / "holder").exists(), "nothing written")
        self.hl.publish(owner, "é" * 256)
        self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Valid, "512 bytes is the bound")

    def test_no_code_path_reads_a_record_to_decide_acquisition(self):
        dpid, dstart = self.dead()
        priors = {"missing": None, "invalid": b"[]", "dead": json.dumps(self.record(dpid, dstart)).encode()}
        for name, raw in priors.items():
            with self.subTest(prior=name):
                if raw is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(self.ldir() / "holder")
                else:
                    self.put(raw)
                with mock.patch.object(self.hl.file_lock, "try_lock_fd", return_value=False):
                    r = self.acquire()
                self.assertIsInstance(r, self.hl.Contended, "the flock decides; the record never grants")

    # ── §3 root and first act ──

    def test_only_the_root_itself_exists_under_the_root_before_acquired(self):
        seen = []
        real = self.hl.file_lock.try_lock_fd

        def look(fd):
            seen.append((self.root.is_dir(), sorted(os.listdir(self.root))))
            return real(fd)
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", look):
            owner = self.owned()
        self.assertEqual(seen, [(True, [])])
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(str(self.ldir()).startswith(str(self.root)))
        self.hl.release(owner)

    def test_two_spellings_of_a_not_yet_existing_root_share_one_key(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        spellings = ["./w", "x/../w", "w/"]
        self.assertFalse(os.path.exists("w"))
        before = set(os.listdir("."))
        self.hl.release(self.owned("x/../w"))
        self.assertEqual(set(os.listdir(".")) - before, {"w"}, "only the root itself: no x/, no sibling")
        self.assertEqual(os.listdir("w"), [], "and nothing under it")
        self.assertEqual(len({self.hl.lock_dir(s) for s in spellings}), 1)
        with self.flock_elsewhere("./w"):
            for s in spellings[1:]:
                self.assertIsInstance(self.acquire(s), self.hl.Contended, s)

    def test_case_aliases_of_one_root_on_a_case_insensitive_filesystem_share_one_key(self):
        upper = self.tmp / "Work"
        upper.mkdir()
        lower = self.tmp / "work"
        if not lower.exists() or not os.path.samefile(upper, lower):
            self.skipTest("this filesystem is case-sensitive")
        self.assertEqual(self.hl.lock_dir(str(upper)), self.hl.lock_dir(str(lower)))
        self.assertNotEqual(self.hl.lock_dir(str(upper)), self.hl.lock_dir(str(self.tmp)))
        with self.flock_elsewhere(upper):
            self.assertIsInstance(self.acquire(lower), self.hl.Contended)
        owner = self.owned(upper)
        self.hl.publish(owner, "routine")
        self.assertIsInstance(self.hl.read_holder(str(lower)), self.hl.Valid)

    def test_a_fork_during_an_acquisition_waits_and_the_child_does_not_keep_the_lock(self):
        code = ("import os, threading, time\n"
                "paused, go = threading.Event(), threading.Event()\nreal = hl.start_time\n"
                "def slow(pid):\n    paused.set()\n    go.wait(10)\n    return real(pid)\n"
                "hl.start_time = slow\nresult = {}\n"
                "t = threading.Thread(target=lambda: result.update(r=hl.acquire(%r, install_id='i')))\nt.start()\n"
                "paused.wait(10)\n"
                "threading.Timer(0.3, go.set).start()\n"
                "child = os.fork()\n"
                "if child == 0:\n    print('child', os.getpid(), flush=True)\n    time.sleep(30)\n    os._exit(0)\n"
                "t.join(10)\nhl.start_time = real\nhl.publish(result['r'].owner, 'routine')\n"
                "print('owner', os.getpid(), flush=True)\ntime.sleep(30)\n" % str(self.root))
        owner = self.run_py(code)
        self.addCleanup(lambda: (owner.kill(), owner.wait(WAIT)))
        lines = dict(read_line(owner.stdout).split() for _ in range(2))
        child_pid, owner_pid = int(lines["child"]), int(lines["owner"])
        self.addCleanup(lambda: self.hl._pid_alive(child_pid) and os.kill(child_pid, signal.SIGKILL))
        os.kill(owner_pid, signal.SIGKILL)
        owner.wait(WAIT)
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and self.hl._pid_alive(owner_pid):
            time.sleep(0.02)
        self.assertTrue(self.hl._pid_alive(child_pid), "the child forked mid-acquire outlives the owner")
        self.assertIsInstance(self.acquire(), self.hl.Acquired, "and does not keep the lock")

    def test_a_forked_child_that_reuses_the_fd_number_never_has_it_closed_by_a_grandchild(self):
        code = ("import os, sys\nr = hl.acquire(%r, install_id='i')\nhl.publish(r.owner, 'routine')\n"
                "lock_fd = r.owner.fd\nchild = os.fork()\n"
                "if child == 0:\n"
                "    spare = os.open(os.devnull, os.O_RDONLY)\n    os.dup2(spare, lock_fd)\n    fd = lock_fd\n"
                "    grand = os.fork()\n"
                "    if grand == 0:\n"
                "        try:\n            os.fstat(fd)\n            state = 'open'\n"
                "        except OSError:\n            state = 'closed'\n"
                "        print('reused', fd == lock_fd, state, flush=True)\n        os._exit(0)\n"
                "    os.waitpid(grand, 0)\n    os._exit(0)\n"
                "os.waitpid(child, 0)\nhl.release(r.owner)\n" % str(self.root))
        proc = self.run_py(code)
        self.addCleanup(lambda: (proc.kill(), proc.wait(WAIT)))
        line = read_line(proc.stdout).split()
        self.assertEqual(line, ["reused", "True", "open"], "the child's own fd, on the old number, survives")
        proc.wait(WAIT)

    def test_a_root_replaced_after_the_flock_check_is_refused_before_acquired(self):
        self.root.mkdir()
        real = self.hl._read_prior

        def replace_root(d):
            os.rename(self.root, self.tmp / "moved")
            self.root.mkdir()
            return real(d)
        with mock.patch.object(self.hl, "_read_prior", replace_root):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "root changed")
        self.assertIsNone(self.hl._OWNER)

    def test_an_unencodable_string_is_invalid_on_read_and_refused_on_publish(self):
        pid, start = self.dead()
        raw = json.dumps(self.record(pid, start)).replace('"routine"', '"\\ud800"').encode()
        self.assertIn(b"\\ud800", raw)
        self.put(raw)
        self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Invalid)
        r = self.acquire()
        self.assertIsInstance(r, self.hl.Acquired, "a dead pid in an invalid record: typed, never raised")
        with self.assertRaises(self.hl.LockError):
            self.hl.publish(r.owner, "\ud800")
        self.assertEqual((self.ldir() / "holder").read_bytes(), raw, "nothing written")

    def test_a_directory_fsync_failure_after_the_rename_is_published_with_durability_uncertain(self):
        owner = self.owned()
        real = os.fsync

        def fail_on_a_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "I/O error")
            return real(fd)
        with mock.patch.object(self.hl.os, "fsync", fail_on_a_directory), \
                self.assertLogs(self.hl._LOG, level="WARNING") as logs:
            got = self.hl.publish(owner, "routine")
        self.assertEqual((type(got).__name__, got.durable), ("Published", False))
        self.assertTrue(owner.published)
        self.assertEqual(self.hl.read_holder(str(self.root)), self.hl.Valid(got.record))
        self.assertIn("durability is uncertain", logs.output[0])
        with self.assertRaises(self.hl.LockError):
            self.hl.publish(owner, "routine")

    def test_a_forked_child_holds_no_lock_fd_and_the_lock_is_free_once_the_owner_dies(self):
        code = ("import os, sys, time\n"
                "def lockfds(lock):\n"
                "    n = 0\n"
                "    for name in os.listdir('/dev/fd'):\n"
                "        try:\n"
                "            st = os.fstat(int(name))\n"
                "        except (OSError, ValueError):\n"
                "            continue\n"
                "        n += (st.st_dev, st.st_ino) == (lock.st_dev, lock.st_ino)\n"
                "    return n\n"
                "r = hl.acquire(%r, install_id=hl.install_id())\nhl.publish(r.owner, 'routine')\n"
                "lock = os.fstat(r.owner.fd)\nassert lockfds(lock) == 1\nchild = os.fork()\n"
                "if child == 0:\n"
                "    print('child', os.getpid(), 'lockfds', lockfds(lock), flush=True)\n"
                "    time.sleep(30)\n    os._exit(0)\n"
                "print('owner', os.getpid(), flush=True)\ntime.sleep(30)\n" % str(self.root))
        owner = self.run_py(code)
        self.addCleanup(lambda: (owner.kill(), owner.wait(WAIT)))
        lines = sorted(read_line(owner.stdout).split() for _ in range(2))
        child_line, owner_line = lines[0], lines[1]
        child_pid, owner_pid = int(child_line[1]), int(owner_line[1])
        self.addCleanup(lambda: self.hl._pid_alive(child_pid) and os.kill(child_pid, signal.SIGKILL))
        self.assertEqual(child_line[3], "0", "the forked child holds no lock descriptor")
        os.kill(owner_pid, signal.SIGKILL)
        owner.wait(WAIT)
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and self.hl._pid_alive(owner_pid):
            time.sleep(0.02)
        self.assertTrue(self.hl._pid_alive(child_pid), "the child outlives the owner")
        r = self.acquire()
        self.assertIsInstance(r, self.hl.Acquired, "the lock is free once the owner dies")
        self.assertEqual(r.prior.record["pid"], owner_pid)

    def test_a_root_swapped_for_a_symlink_between_mkdir_and_flock_is_refused(self):
        real = self.hl.file_lock.try_lock_fd
        key = self.ldir()

        def swap(fd):
            os.rename(self.root, self.tmp / "elsewhere")
            os.symlink(self.tmp / "elsewhere", self.root)
            return real(fd)
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", swap):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "root changed")
        self.assertTrue(self.lock_free(directory=key))

    def test_every_refusal_path_leaves_the_root_and_the_record_byte_identical(self):
        self.root.mkdir()
        (self.root / "theirs.txt").write_text("x")
        pid, start = self.proc()
        live = self.record(pid, start)

        def check(name, setup, call):
            with self.subTest(path=name):
                setup()
                holder = self.ldir() / "holder"
                before = (snapshot(str(self.root)), holder.read_bytes() if holder.is_file() else None)
                with contextlib.suppress(self.hl.LockError, RuntimeError):
                    call()
                after = (snapshot(str(self.root)), holder.read_bytes() if holder.is_file() else None)
                self.assertEqual(after, before)
                self.assertEqual(sorted(n for n in os.listdir(self.ldir()) if not n.startswith("waiting.")),
                                 sorted(n for n in ("lock", "holder") if (self.ldir() / n).exists()))
                self.assertIsNone(self.hl._OWNER)
        check("held", lambda: self.put(live), self.acquire)
        with self.flock_elsewhere():
            check("contended", lambda: None, self.acquire)
            check("timed out", lambda: None,
                  lambda: self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 0.2, poll=0.05))
        check("unreadable", lambda: (os.unlink(self.ldir() / "holder"), os.mkdir(self.ldir() / "holder")),
              self.acquire)
        os.rmdir(self.ldir() / "holder")

        def legacy():
            (self.root / "state").mkdir(exist_ok=True)
            (self.root / "state" / "connector.pid").write_text("%d\n" % pid)
        check("legacy running", legacy, self.acquire)
        os.unlink(self.root / "state" / "connector.pid")
        real = self.hl._read_prior
        with mock.patch.object(self.hl, "_read_prior", side_effect=RuntimeError("fault after the flock")):
            check("fault", lambda: None, self.acquire)
        self.assertTrue(self.lock_free())
        self.assertIs(real, self.hl._read_prior)

    def test_the_first_act_contract_passes_against_the_stub_caller(self):
        self.real_home()
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(self.stub_contract()))
        self.assertTrue(result.wasSuccessful(), [str(e[1])[-1500:] for e in result.failures + result.errors])
        self.assertEqual(result.testsRun, 7)

    def test_the_first_act_contract_sigterm_case_holds_for_a_signal_right_after_acquired(self):
        """CI (Linux): SIGTERM landed between the stub's `acquired` line and its try block, so its
        handler's exit skipped release and the record stayed; the stub lingers there to pin it."""
        self.real_home()
        case = self.stub_contract("--linger", "0.5")("test_first_act_sigterm_ends_the_caller_and_frees_the_lock")
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(case)
        self.assertTrue(result.wasSuccessful(), [str(e[1])[-1500:] for e in result.failures + result.errors])

    def test_a_failing_contract_case_prints_fail_diag_with_the_callers_exit_and_output(self):
        """CI shows only lines matching FAIL: a failing case names each caller's exit code and output."""
        self.real_home()
        case = self.stub_contract("--by-path")("test_first_act_lands_on_the_held_root_when_the_path_is_replaced")
        case.fa_quiet = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(case)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines and all(line.startswith("FAIL-DIAG: ") for line in lines), lines)
        self.assertIn("test_first_act_lands_on_the_held_root_when_the_path_is_replaced rc=", lines[0])
        self.assertIn("acquired", lines[0], "the head of the caller's output")
        self.assertIn(lines[0], result.failures[0][1], "and the same line in the failure message")

    def test_the_first_act_contract_pins_publish_before_any_child_is_spawned(self):
        self.real_home()
        case = self.stub_contract("--spawn-before-publish")("test_first_act_publish_comes_before_any_child_is_spawned")
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(case)
        self.assertEqual(len(result.failures), 1, "a spawn before publish fails the contract")
        self.assertIn("before the record existed", result.failures[0][1])

    def test_the_first_act_contract_pins_the_first_act_on_the_held_root(self):
        """Review schedule: "Replacing the root after the final successful root check while driving that
        committed stub produced: acquired | old_state False new_state True checks 2" -- add the
        replacement schedule to the reusable contract, so a pathname first act fails it."""
        self.real_home()
        case = self.stub_contract("--by-path")("test_first_act_lands_on_the_held_root_when_the_path_is_replaced")
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(case)
        self.assertEqual(len(result.failures), 1, "a first act by pathname fails the contract")
        self.assertIn("nothing on the new directory at the path", result.failures[0][1])

    def test_the_first_act_contract_pins_no_holder_lock_call_in_a_signal_handler(self):
        """A caller whose SIGTERM handler calls release (instead of setting a flag for its main loop)
        never ends on SIGTERM, and the contract fails it."""
        self.real_home()
        case = self.stub_contract("--release-in-handler")("test_first_act_sigterm_ends_the_caller_and_frees_the_lock")
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(case)
        self.assertEqual(len(result.failures), 1, "a handler that calls holder_lock fails the contract")
        self.assertIn("did not end on SIGTERM", result.failures[0][1])

    def stub_contract(self, *extra):
        kind, mod = self.kind, self.hl

        class Stub(FirstActContract, unittest.TestCase):
            module, module_kind = mod, kind
            fa_quiet = bool(extra) and "--linger" not in extra

            def caller(self, root, *more):
                return [str(REPO / "tests" / "_helpers" / "holder_lock_stub.py"), "--module", kind, "--work", root] + list(extra)
        return Stub

    # ── §4 install boundary and the legacy pidfile ──

    def test_install_id_is_the_location_only_and_is_not_compared_on_acquire(self):
        where = os.path.realpath(os.path.dirname(self.hl.__file__))
        dist = "sutando-src" if self.hl is SRC else "ag2-sparrow"
        self.assertEqual(self.hl.install_id(), "%s@%s" % (dist, where))
        pid, start = self.dead()
        self.put(self.record(pid, start, install_id="elsewhere@/old/path"))
        self.hl.release(self.owned())
        pid, start = self.proc()
        self.put(self.record(pid, start, install_id=self.hl.install_id()))
        self.assertIsInstance(self.acquire(), self.hl.Held, "the same install is not a pass")

    def test_a_start_against_a_live_holder_is_held_and_explains(self):
        pid, start = self.proc()
        self.put(self.record(pid, start, adapter="codex"))
        r = self.acquire()
        self.assertEqual(r.reason, "live holder")
        self.assertEqual((r.holder.record["adapter"], r.holder.record["pid"]), ("codex", pid))

    def legacy(self, text, age=0):
        p = self.root / "state" / "connector.pid"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        when = time.time() - age
        os.utime(p, (when, when))
        return str(p)

    def test_a_live_or_unobserved_legacy_pid_refuses_and_explains(self):
        pid, _ = self.proc()
        path = self.legacy("%d\n" % pid)
        self.assertEqual(self.acquire(), self.hl.LegacyRunning(pid, path))
        self.assertTrue(self.lock_free(), "unlocked")
        self.assertFalse((self.ldir() / "holder").exists())

    def test_a_fresh_empty_or_malformed_legacy_file_refuses_and_a_dead_or_absent_one_is_ignored(self):
        for text in ("", "abc", "12 34\n", "-5\n"):
            with self.subTest(text=text):
                path = self.legacy(text, age=0)
                self.assertEqual(self.acquire(), self.hl.LegacyRunning(None, path))
        pid, _ = self.dead()
        self.legacy("%d\n" % pid)
        self.hl.release(self.owned())
        os.unlink(self.root / "state" / "connector.pid")
        self.hl.release(self.owned())

    def test_a_stale_empty_or_malformed_legacy_file_is_ignored_and_status_warns(self):
        for text in ("", "abc"):
            with self.subTest(text=text):
                path = self.legacy(text, age=31)
                self.hl.release(self.owned())
                st = self.hl.status(str(self.root))
                self.assertTrue([w for w in st["warnings"] if path in w], st)
        self.legacy("", age=0)
        self.assertFalse(self.hl.status(str(self.root))["warnings"], "a fresh one is not stale")

    def test_the_module_never_writes_or_removes_the_legacy_file(self):
        pid, _ = self.dead()
        path = self.legacy("%d\n" % pid, age=100)
        before = (Path(path).read_bytes(), os.stat(path).st_ino, os.stat(path).st_mtime)
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.hl.status(str(self.root))
        self.hl.release(owner)
        self.legacy("", age=100)
        self.hl.release(self.owned())
        self.assertEqual(Path(path).read_bytes(), b"")
        self.assertEqual((before[1], ), (os.stat(path).st_ino, ))
        tree = ast.parse(Path(self.hl.__file__).read_text())
        legacy_uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "LEGACY_PIDFILE"]
        self.assertTrue(legacy_uses)
        source = Path(self.hl.__file__).read_text()
        body = source[source.index("def _legacy_finding"):source.index("# ── acquire, wait")]
        self.assertNotRegex(body, r'open\([^)]*"[wa]|unlink|replace|rename', "read-only")

    # ── §5 location ──

    def test_lock_dir_is_a_function_of_realpath_and_uid_only_and_ignores_every_env_var(self):
        self.real_home()
        real_home = pwd.getpwuid(os.geteuid()).pw_dir
        self.root.mkdir()
        base = self.hl.lock_dir(str(self.root))
        self.assertTrue(base.startswith(os.path.join(real_home, ".local", "state", "ag2-sparrow", "locks") + os.sep))
        env = {"HOME": "/nonexistent", "XDG_STATE_HOME": "/x", "XDG_RUNTIME_DIR": "/y", "SPARROW_LOCK_DIR": "/z",
               "TMPDIR": "/w"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.hl.lock_dir(str(self.root)), base)
        os.symlink(self.tmp, self.tmp.parent / ("link-" + self.tmp.name))
        self.addCleanup(os.unlink, self.tmp.parent / ("link-" + self.tmp.name))
        self.assertEqual(self.hl.lock_dir(str(self.tmp.parent / ("link-" + self.tmp.name) / "work")), base)
        (self.tmp / "other").mkdir()
        self.assertNotEqual(self.hl.lock_dir(str(self.tmp / "other")), base)

    def test_two_launch_contexts_with_different_environments_resolve_one_lock_dir(self):
        root = str(self.root)
        self.real_home()
        self.forget_lock_dir(root)
        code = ("import sys\nr = hl.acquire(%r, install_id=hl.install_id())\n"
                "print(hl.lock_dir(%r), type(r).__name__, flush=True)\n"
                "if isinstance(r, hl.Acquired):\n    hl.publish(r.owner, 'stub')\n    sys.stdin.read()\n"
                "    hl.release(r.owner)\n" % (root, root))
        first = subprocess.Popen([sys.executable, "-c", BOOT, self.kind, "-", code], env={},
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (first.kill(), first.wait(WAIT)))
        a_dir, a_kind = read_line(first.stdout).split()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp / "fakehome"), "XDG_STATE_HOME": str(self.tmp / "xdg"),
               "XDG_RUNTIME_DIR": str(self.tmp / "run"), "SPARROW_LOCK_DIR": str(self.tmp / "s"), "LANG": "C"}
        second = subprocess.run([sys.executable, "-c", BOOT, self.kind, "-", code], env=env, capture_output=True,
                                text=True, timeout=WAIT * 3, stdin=subprocess.DEVNULL)
        b_dir, b_kind = second.stdout.split()
        first.stdin.close()
        first.wait(WAIT)
        self.assertEqual((a_dir, a_kind), (b_dir, "Acquired"))
        self.assertIn(b_kind, ("Held", "Contended"))

    def test_a_root_containing_the_lock_dir_is_refused(self):
        for root in (self.home, self.tmp):
            with self.subTest(root=root):
                with self.assertRaises(self.hl.LockError) as e:
                    self.acquire(root)
                self.assertEqual(str(e.exception), "work root contains the lock directory")

    def test_a_network_or_fuse_lock_dir_is_refused(self):
        for fstype, refused in (("nfs", True), ("fuse.sshfs", True), ("smbfs", True), ("nonlocal:afpfs", True),
                                ("apfs", False), ("ext4", False), (None, False)):
            with self.subTest(fstype=fstype), mock.patch.object(self.hl, "_statfs_type", return_value=fstype):
                if refused:
                    with self.assertRaises(self.hl.LockError) as e:
                        self.acquire()
                    self.assertEqual(str(e.exception), "not a local filesystem")
                else:
                    self.hl.release(self.owned())

    def test_the_readmes_state_the_supported_upgrade_paths_and_the_manual_stop_for_everything_else(self):
        for readme in PKG_READMES:
            with self.subTest(readme=readme.relative_to(REPO)):
                text = " ".join(readme.read_text().split())
                self.assertIn("Supported upgrade paths: the Hatch installer and the keepawake self-upgrade", text)
                for phrase in ("pip", "pipx", "rollback", "supervisor", "work root the upgrader does not know",
                               "stop the adapters by hand"):
                    self.assertIn(phrase, text)

    def test_the_readmes_document_the_network_home_refusal(self):
        for readme in PKG_READMES:
            with self.subTest(readme=readme.relative_to(REPO)):
                text = readme.read_text()
                self.assertIn('LockError("not a local filesystem")', text)
                self.assertIn("local home", text)
                self.assertIn("stop → install → start", text)

    # ── §6 crash matrix ──

    def test_an_owner_killed_at_every_boundary_is_replaced_by_a_successor_whose_outcome_matches_the_row(self):
        dpid, dstart = self.dead()
        prior = self.record(dpid, dstart)
        with self.subTest(row="root mkdir / flock / prior read, before publish"):
            self.root.mkdir()
            data = self.put(prior)
            with self.flock_elsewhere():
                self.assertEqual(self.acquire(), self.hl.Contended(self.hl.Valid(json.loads(data))))
            r = self.acquire()
            self.assertEqual(r.prior, self.hl.Valid(json.loads(data)))
            self.hl.release(r.owner)
        with self.subTest(row="record temp written"):
            (self.ldir() / ".holder.4242.deadbeef.tmp").write_text("{")
            owner = self.owned()
            self.hl.publish(owner, "routine")
            self.assertEqual(sorted(os.listdir(self.ldir())), ["holder", "lock"])
            self.hl.release(owner)
        with self.subTest(row="published, then serving, killed"):
            code = ("r = hl.acquire(%r, install_id=hl.install_id())\nhl.publish(r.owner, 'routine')\n"
                    "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(30)\n" % str(self.root))
            child = self.run_py(code)
            self.addCleanup(lambda: (child.kill(), child.wait(WAIT)))
            pid = int(read_line(child.stdout))
            os.kill(pid, signal.SIGKILL)
            child.wait(WAIT)
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline and self.hl._pid_alive(pid):
                time.sleep(0.02)
            r = self.acquire()
            self.assertIsInstance(r, self.hl.Acquired)
            self.assertEqual(r.prior.record["pid"], pid)
            self.hl.release(r.owner)
        with self.subTest(row="clean exit, record removed, before close"):
            os.unlink(self.ldir() / "holder")
            with self.flock_elsewhere():
                self.assertEqual(self.acquire(), self.hl.Contended(self.hl.Missing()))
            self.assertEqual(self.acquire().prior, self.hl.Missing())

    # ── §7 both copies ──

    # ── error paths, each driven for real ──

    def denied(self, path):
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o700)
        if os.access(path, os.R_OK):
            self.skipTest("permissions are not enforced for this user")

    def test_identity_helpers_report_unobservable_instead_of_raising(self):
        self.real_home()
        with mock.patch.object(self.hl, "pwd", None):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl._home()
        self.assertEqual(str(e.exception), self.hl.UNSUPPORTED)
        with mock.patch.object(self.hl.subprocess, "run", side_effect=OSError(2, "no ps")):
            self.assertIsNone(self.hl.start_time(os.getpid()))
        with mock.patch.object(self.hl.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "not a date at all\n", "")):
            self.assertIsNone(self.hl.start_time(os.getpid()))
        with mock.patch.object(self.hl.os, "kill", side_effect=PermissionError(1, "not permitted")):
            self.assertIs(self.hl._pid_alive(12345), True, "another user's live process")
        self.assertIsNone(self.hl._pid_alive(2 ** 70), "a pid that cannot exist is unobservable")

    def test_the_filesystem_type_is_read_from_the_mount_table_or_statfs(self):
        mounts = ("rootfs / ext4 rw 0 0\nserver:/zz-home /zz-home nfs4 rw 0 0\n"
                  "fuse /zz-mnt/my\\040dir fuse.sshfs rw 0 0\n")
        with mock.patch.object(self.hl.sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data=mounts)):
            self.assertEqual(self.hl._statfs_type("/zz-home/u/.local/state"), "nfs4")
            self.assertEqual(self.hl._statfs_type("/zz-mnt/my dir/x"), "fuse.sshfs")
            self.assertEqual(self.hl._statfs_type("/zz-elsewhere/x"), "ext4")
        with mock.patch.object(self.hl.sys, "platform", "linux"), \
                mock.patch("builtins.open", side_effect=OSError(13, "denied")):
            self.assertIsNone(self.hl._statfs_type("/home"))
        self.assertIsNone(self.hl._darwin_statfs_type(str(self.tmp / "absent" / "x")), "statfs fails: unknown")
        import ctypes
        with mock.patch.object(ctypes, "CDLL", side_effect=OSError("no libc")):
            self.assertIsNone(self.hl._darwin_statfs_type(str(self.tmp)))

    def test_an_invalid_record_that_names_no_pid_or_cannot_be_validated_is_overwritten_typed(self):
        self.put(b"[1, 2]")
        self.assertEqual(self.hl._pids_in(b"[1, 2]"), [])
        self.hl.release(self.owned())
        with mock.patch.object(self.hl, "_record_problem", side_effect=ValueError("odd")):
            got = self.hl._classify(b"{}")
        self.assertIsInstance(got, self.hl.Invalid)
        with mock.patch.object(self.hl, "_read_prior", side_effect=ValueError("odd")):
            self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Invalid)
            r = self.acquire()
        self.assertIsInstance(r, self.hl.Acquired, "an undecodable prior is Invalid, never raised")
        self.hl.release(r.owner)

    def test_read_holder_and_status_report_an_unreadable_root_or_lock_dir(self):
        self.root.mkdir()
        outer = self.tmp / "outer"
        (outer / "inner").mkdir(parents=True)
        self.denied(outer)
        self.assertIsInstance(self.hl.read_holder(str(outer / "inner")), self.hl.Invalid)
        os.chmod(outer, 0o700)
        st = self.hl.status(str(self.tmp / "never-created"))
        self.assertEqual((st["lock_dir"], st["record"]), (None, {"kind": "missing"}))
        d = self.ldir()
        d.mkdir(parents=True)
        (d / "holder").write_text("{}")
        self.denied(d)
        st = self.hl.status(str(self.root))
        self.assertEqual(st["record"]["kind"], "unreadable")
        self.assertEqual((st["waiters"], st["stale_waiters"]), ([], []))
        os.chmod(d, 0o700)
        (d / "waiting.99999999").mkdir()
        self.assertEqual(self.hl.status(str(self.root))["stale_waiters"], [99999999], "an unreadable waiter")

    def test_an_unreadable_legacy_pidfile_refuses(self):
        p = self.root / "state" / "connector.pid"
        p.parent.mkdir(parents=True)
        p.write_text("123\n")
        self.denied(p)
        r = self.acquire()
        self.assertEqual(r, self.hl.LegacyRunning(None, str(p)))

    def test_a_root_removed_or_a_start_time_missing_after_the_flock_refuses(self):
        real = self.hl.file_lock.try_lock_fd

        def remove_root(fd):
            os.rmdir(self.root)
            return real(fd)
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", remove_root):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "root changed")
        with mock.patch.object(self.hl, "start_time", return_value=None):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire()
        self.assertEqual(str(e.exception), "cannot read this process's start time")

    def test_acquire_with_a_timeout_waits_and_wait_returns_at_once_when_free(self):
        with self.flock_elsewhere():
            began = time.monotonic()
            self.assertIsInstance(self.acquire(timeout=0.2), self.hl.TimedOut)
            self.assertLess(time.monotonic() - began, 2)
        r = self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 5)
        self.assertIsInstance(r, self.hl.Acquired)
        self.hl.release(r.owner)

    def test_a_waiting_file_removed_by_someone_else_is_not_an_error(self):
        def remove_mine(elapsed, holder):
            os.unlink(self.ldir() / ("waiting.%d" % os.getpid()))
            raise KeyError("stop")
        with self.flock_elsewhere(), self.assertRaises(KeyError):
            self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 5, on_tick=remove_mine)
        self.assertIsNone(self.hl._WAITING)

    def test_publish_and_release_from_another_pid_or_after_a_write_failure(self):
        owner = self.owned()
        stranger = dataclasses.replace(owner, pid=owner.pid + 1)
        for call in (self.hl.publish, self.hl.release):
            with self.subTest(call=call.__name__), self.assertRaises(self.hl.LockError) as e:
                call(*((stranger, "routine") if call is self.hl.publish else (stranger,)))
            self.assertEqual(str(e.exception), "not the owner")
        (self.ldir() / ".holder.1.deadbeef.tmp").mkdir()
        (self.ldir() / ".holder.1.deadbeef.tmp" / "x").write_text("")
        with mock.patch.object(self.hl.os, "replace", side_effect=OSError(28, "No space left")), \
                mock.patch.object(self.hl.os, "unlink", side_effect=OSError(1, "not permitted")):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.publish(owner, "routine")
        self.assertIn("cannot write the holder record", str(e.exception))
        self.assertFalse(owner.published, "the owner is still valid")
        self.assertFalse((self.ldir() / "holder").exists())
        self.hl.publish(owner, "routine")
        with mock.patch.object(self.hl, "_read_prior", side_effect=OSError(5, "I/O error")):
            self.assertEqual(self.hl.release(owner), self.hl.Released(False))
        self.assertTrue(self.lock_free(), "unlocked even when the record could not be read")

    def test_the_child_side_of_a_fork_closes_only_its_own_lock_descriptor(self):
        owner = self.owned()
        other = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: contextlib.suppress(OSError) and os.close(other))
        mismatched = dataclasses.replace(owner, fd=other, root_fd=-1)
        self.hl._OWNER = mismatched
        self.hl._drop_in_child()
        os.fstat(other)
        self.assertIsNone(self.hl._OWNER, "the owner is dropped")
        self.assertFalse(self.hl._ACQUIRING.locked())
        closed = os.dup(owner.fd)
        os.close(closed)
        gone = dataclasses.replace(owner, fd=closed, root_fd=-1)
        self.hl._OWNER = gone
        self.hl._drop_in_child()
        self.hl._OWNER = owner
        self.hl._drop_in_child()
        with self.assertRaises(OSError):
            os.fstat(owner.fd)
        owner.released = True
        self.hl._drop_in_child()
        self.assertIsNone(self.hl._OWNER, "a child of a child with no owner does nothing")

    # ── second code review: the held root, fork during release, the inherited guard ──

    def test_stable_root_identity_holds_through_the_consumers_first_act(self):
        """Review schedule: "I replaced the root immediately after that second check returned true, before
        `_attempt` constructed the owner ... first=Acquired stable_identity_matches_path=False
        second=Acquired" -- keep a stable root capability through the returned lease and make consumers
        use it, then pin replacement after the final check and after publish/at the first consumer act."""
        self.root.mkdir()
        real, calls = self.hl._root_unchanged, []

        def check_then_swap(path, root, fd):
            ok = real(path, root, fd)
            calls.append(ok)
            if len(calls) == 2:
                os.rename(self.root, self.tmp / "moved")
                self.root.mkdir()
            return ok
        with mock.patch.object(self.hl, "_root_unchanged", check_then_swap):
            r = self.acquire()
        self.assertEqual(len(calls), 2, "the final check is the last step before Acquired")
        self.assertIsInstance(r, self.hl.Acquired, "the swap came after the last check")
        held, moved = os.fstat(r.owner.root_fd), os.stat(self.tmp / "moved")
        self.assertEqual((held.st_dev, held.st_ino), (moved.st_dev, moved.st_ino), "the owner holds its inode")
        for name in ("tasks", "results", "state"):
            os.mkdir(name, 0o700, dir_fd=r.owner.root_fd)
        self.hl.publish(r.owner, "routine")
        self.assertEqual(sorted(os.listdir(self.tmp / "moved")), ["results", "state", "tasks"],
                         "the consumer's first act lands in the original inode")
        self.assertEqual(os.listdir(self.root), [], "nothing on the new directory")
        self.assertIs(self.hl.status(str(self.tmp / "moved"))["root_unchanged"], False,
                      "status under the owner sees that its path now names another directory")
        code = ("r = hl.acquire(%r, install_id='i')\nprint(type(r).__name__, hl.lock_dir(%r), flush=True)\n"
                % (str(self.root), str(self.root)))
        other = self.run_py(code)
        self.addCleanup(lambda: (other.kill(), other.wait(WAIT)))
        kind, other_dir = read_line(other.stdout).split()
        self.assertEqual(kind, "Acquired", "the second acquirer at the path holds its own lock")
        self.assertNotEqual(other_dir, r.owner.directory, "on the new inode")
        other.wait(WAIT)
        self.hl.release(r.owner)
        with self.assertRaises(OSError):
            os.fstat(r.owner.root_fd)

    def test_a_fork_during_release_cannot_keep_the_flock(self):
        """Review schedule: "`release` sets `owner.released = True` before it unlocks/closes ... I paused
        exactly between those operations, forked a sleeping child, and terminated the owner before
        unlock: release_paused=True while_child_alive=Contended after_child_exit=Acquired"."""
        seams = {
            "the review's seam, before the unlock": (
                "real = hl.file_lock.unlock_fd\n"
                "def seam(fd):\n    paused.set()\n    go.wait(10)\n    return real(fd)\n"
                "hl.file_lock.unlock_fd = seam\n"),
            "between unlock and close": (
                "real = hl.file_lock.unlock_fd\n"
                "def seam(fd):\n    out = real(fd)\n    paused.set()\n    go.wait(10)\n    return out\n"
                "hl.file_lock.unlock_fd = seam\n"),
            "between close and the released flag": (
                "real = hl.os.close\n"
                "def seam(fd):\n    real(fd)\n    if fd == r.owner.root_fd:\n        paused.set()\n        go.wait(10)\n"
                "hl.os.close = seam\n"),
        }
        for name, patch in seams.items():
            with self.subTest(seam=name):
                root = self.tmp / name.replace(" ", "-")
                code = ("import os, threading, time\nr = hl.acquire(%r, install_id='i')\n"
                        "hl.publish(r.owner, 'routine')\npaused, go = threading.Event(), threading.Event()\n%s"
                        "threading.Thread(target=hl.release, args=(r.owner,), daemon=True).start()\n"
                        "paused.wait(10)\nprint('paused', os.getpid(), flush=True)\n"
                        "child = os.fork()\n"
                        "if child == 0:\n    print('child', os.getpid(), flush=True)\n    time.sleep(30)\n"
                        "    os._exit(0)\n"
                        "time.sleep(30)\n" % (str(root), patch))
                owner = self.run_py(code)
                self.addCleanup(lambda o=owner: (o.kill(), o.wait(WAIT)))
                _, pid = read_line(owner.stdout).split()
                children = []
                line = (read_line(owner.stdout, 1.0, default="") or "").split()
                if line and line[0] == "child":
                    children.append(int(line[1]))
                for child in children:
                    self.addCleanup(lambda c=child: self.hl._pid_alive(c) and os.kill(c, signal.SIGKILL))
                os.kill(int(pid), signal.SIGKILL)
                owner.wait(WAIT)
                deadline = time.monotonic() + WAIT
                while time.monotonic() < deadline and self.hl._pid_alive(int(pid)):
                    time.sleep(0.02)
                got = self.acquire(root)
                self.assertIsInstance(got, self.hl.Acquired,
                                      "a successor acquires at once; no forked child holds the lock (%s)" % children)
                self.hl.release(got.owner)

    def test_a_child_cannot_inherit_any_module_lock_in_an_unusable_state(self):
        """Review schedule: "If another thread is in one of the `_GUARD` regions when the fork happens, the
        vanished thread remains its owner and the child's next `acquire` blocks in `_prepare`:
        guard_inherited_locked=True child_acquire=TIMEOUT"."""
        for lock in ("_GUARD", "_ACQUIRING"):
            with self.subTest(held=lock):
                root = self.tmp / ("child-" + lock.strip("_").lower())
                code = ("import os, threading, time, signal\nheld = threading.Event()\n"
                        "def hold():\n    with hl.%s:\n        held.set()\n        time.sleep(0.5)\n"
                        "threading.Thread(target=hold, daemon=True).start()\nheld.wait(10)\n"
                        "child = os.fork()\n"
                        "if child == 0:\n"
                        "    signal.alarm(5)\n"
                        "    r = hl.acquire(%r, install_id='i')\n"
                        "    hl.publish(r.owner, 'routine')\n"
                        "    hl.release(r.owner)\n"
                        "    w = hl.wait(%r, install_id='i', deadline=time.monotonic() + 2)\n"
                        "    hl.release(w.owner)\n"
                        "    print('child', type(r).__name__, type(w).__name__, flush=True)\n    os._exit(0)\n"
                        "os.waitpid(child, 0)\n" % (lock, str(root), str(root)))
                proc = self.run_py(code)
                self.addCleanup(lambda p=proc: (p.kill(), p.wait(WAIT)))
                self.assertEqual(read_line(proc.stdout).split(), ["child", "Acquired", "Acquired"],
                                 "acquire, publish, release and wait all proceed in the child")
                proc.wait(WAIT)

    def test_a_case_alias_of_the_home_as_the_root_is_refused_before_anything_is_created(self):
        home = self.tmp / "Hx"
        home.mkdir()
        alias = self.tmp / "hx"
        if not os.path.samefile(home, alias) if alias.exists() else True:
            self.skipTest("this filesystem is case-sensitive")
        with mock.patch.object(self.hl, "_home", lambda: str(home)):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(alias)
        self.assertEqual(str(e.exception), "work root contains the lock directory")
        self.assertEqual(os.listdir(home), [], "no .local/ under the root")

    def test_a_failing_directory_scan_before_the_rename_is_a_lock_error(self):
        owner = self.owned()
        real = os.listdir

        def failing(path="."):
            if path == owner.dir_fd or (not isinstance(path, int) and os.fspath(path) == owner.directory):
                raise OSError(5, "I/O error")
            return real(path)
        with mock.patch.object(self.hl.os, "listdir", failing):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.publish(owner, "routine")
        self.assertIn("cannot write the holder record", str(e.exception))
        self.assertFalse(owner.published)
        self.assertFalse((self.ldir() / "holder").exists())

    # ── third code review ──

    def pause_after_the_owner_check(self):
        """The real owner check, then a pause in any thread but this one until `go` is set."""
        checked, go, main = threading.Event(), threading.Event(), threading.current_thread()
        real = self.hl._check_owner

        def paused(owner):
            real(owner)
            if threading.current_thread() is not main:
                checked.set()
                go.wait(10)
        patcher = mock.patch.object(self.hl, "_check_owner", paused)
        patcher.start()
        self.addCleanup(patcher.stop)
        return checked, go

    def test_a_second_release_racing_the_first_never_touches_reused_descriptors(self):
        """Review schedule: "I used a barrier immediately after the real check so two threads observed
        released=False, then let one finish first ... I reopened /dev/null on the released owner's
        numeric lock/root fds; the second release closed both unrelated descriptors"."""
        owner = self.owned()
        self.hl.publish(owner, "routine")
        checked, go = self.pause_after_the_owner_check()
        results = []

        def second():
            try:
                results.append(self.hl.release(owner))
            except self.hl.LockError as e:
                results.append("LockError: %s" % e)
        t = threading.Thread(target=second)
        t.start()
        self.assertTrue(checked.wait(10))
        self.assertEqual(self.hl.release(owner), self.hl.Released(True))
        lock_n, root_n = owner.fd, owner.root_fd
        null = os.open(os.devnull, os.O_RDONLY)
        for n in (lock_n, root_n):
            if n != null:
                os.dup2(null, n)
        if null not in (lock_n, root_n):
            os.close(null)
        def close_quietly():
            for n in (lock_n, root_n):
                with contextlib.suppress(OSError):
                    os.close(n)
        self.addCleanup(close_quietly)
        go.set()
        t.join(10)
        self.assertEqual(results, ["LockError: released"])
        os.fstat(lock_n)
        os.fstat(root_n)
        again = self.owned()
        self.hl.publish(again, "routine")
        self.race_two_releases_inside_the_critical_section(again)

    def test_a_released_handle_cannot_publish_over_its_replacements_record(self):
        """Review schedule: "release completed, a replacement owner naturally reused the fd numbers
        and published adapter=new, then the old released handle successfully overwrote that committed
        record with adapter=stale"."""
        old = self.owned()
        checked, go = self.pause_after_the_owner_check()
        results = []

        def stale():
            try:
                results.append(self.hl.publish(old, "stale"))
            except self.hl.LockError as e:
                results.append("LockError: %s" % e)
        t = threading.Thread(target=stale)
        t.start()
        self.assertTrue(checked.wait(10))
        self.hl.release(old)
        new = self.owned()
        if old.fd not in (new.fd, new.root_fd):
            os.dup2(new.fd, old.fd)

            def close_alias():
                with contextlib.suppress(OSError):
                    os.close(old.fd)
            self.addCleanup(close_alias)
        self.hl.publish(new, "new")
        go.set()
        t.join(10)
        self.assertEqual(results, ["LockError: released"])
        self.assertEqual(self.hl.read_holder(str(self.root)).record["adapter"], "new")

    def test_a_symlinked_ancestor_pointing_inside_the_root_is_refused(self):
        """Review schedule: "With the service home's .local symlinked to <root>/descendant, both modules
        returned Acquired and created the lock tree physically under the work root"."""
        root = self.tmp / "r"
        (root / "descendant").mkdir(parents=True)
        home = self.tmp / "h"
        home.mkdir()
        os.symlink(root / "descendant", home / ".local")
        with mock.patch.object(self.hl, "_home", lambda: str(home)):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(root)
        self.assertEqual(str(e.exception), "work root contains the lock directory")
        self.assertEqual(os.listdir(root / "descendant"), [], "nothing created under the root")
        os.unlink(home / ".local")
        os.symlink(root, home / ".local")
        with mock.patch.object(self.hl, "_home", lambda: str(home)), self.assertRaises(self.hl.LockError):
            self.acquire(root)

    def test_a_contended_result_reports_the_record_of_the_lock_it_contended_on(self):
        """Review schedule: "Replacing the root at that seam produced, for both modules:
        result=Contended holder=Missing old_record=Valid"."""
        self.root.mkdir()
        pid, start = self.proc()
        data = self.put(self.record(pid, start))
        real = self.hl.file_lock.try_lock_fd

        def replace_then_lock(fd):
            os.rename(self.root, self.tmp / "moved")
            self.root.mkdir()
            return real(fd)
        with self.flock_elsewhere(), mock.patch.object(self.hl.file_lock, "try_lock_fd", replace_then_lock):
            r = self.acquire()
        self.assertEqual(r, self.hl.Contended(self.hl.Valid(json.loads(data))))

    def test_containment_edges_and_a_handle_whose_fd_is_no_longer_the_lock(self):
        top = os.stat(os.sep)
        fd = os.open(str(self.tmp), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertTrue(self.hl._fd_within(fd, (top.st_dev, top.st_ino)), "everything lies under /")
        self.assertFalse(self.hl._fd_within(fd, (0, 0)), "and under no other directory that is not an ancestor")
        owner = self.owned()
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, owner.fd)
        os.close(null)
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "released", "a handle whose fd is not the lock never writes")
        owner.released, self.hl._OWNER = True, None
        os.close(owner.fd)
        os.close(owner.root_fd)

    def test_failed_publishes_leave_at_most_one_temp_and_the_next_publish_removes_it(self):
        """Review schedule: "Three retryable failures left counts [1, 2, 3]"; the temp name is fixed
        per owner and opened with O_TRUNC, so a retry reuses it."""
        owner = self.owned()
        counts = []
        for _ in range(3):
            with mock.patch.object(self.hl.os, "replace", side_effect=OSError(5, "I/O error")), \
                    mock.patch.object(self.hl.os, "unlink", side_effect=OSError(1, "not permitted")):
                with self.assertRaises(self.hl.LockError):
                    self.hl.publish(owner, "routine")
            counts.append(len([n for n in os.listdir(self.ldir()) if n.startswith(".holder.")]))
        self.assertEqual(counts, [1, 1, 1])
        self.assertFalse((self.ldir() / "holder").exists(), "nothing published")
        self.assertTrue(self.hl.publish(owner, "routine").durable)
        self.assertEqual([n for n in os.listdir(self.ldir()) if n.startswith(".holder.")], [],
                         "the next publish that succeeds consumes it")

    def test_an_earlier_owners_stuck_temp_stays_while_removal_fails_and_goes_once_it_can(self):
        owner = self.owned()
        (self.ldir() / ".holder.1-2.tmp").write_bytes(b"x")
        real_unlink = os.unlink

        def unlink_all_but_the_temp(p, *a, **k):
            if os.path.basename(os.fspath(p)).startswith(".holder."):
                raise OSError(1, "not permitted")
            return real_unlink(p, *a, **k)
        with mock.patch.object(self.hl.os, "unlink", unlink_all_but_the_temp), \
                self.assertLogs(self.hl._LOG, level="WARNING") as logs:
            self.assertTrue(self.hl.publish(owner, "routine").durable, "a stuck temp does not fail publish")
        self.assertIn("could not be removed", logs.output[0])
        self.assertEqual([n for n in os.listdir(self.ldir()) if n.startswith(".holder.")], [".holder.1-2.tmp"])
        self.hl.release(owner)
        self.hl.publish(self.owned(), "routine")
        self.assertEqual([n for n in os.listdir(self.ldir()) if n.startswith(".holder.")], [],
                         "the first publish that can remove it does")

    # ── the code review at ceb81d5 ──

    def test_a_signal_handler_calling_release_mid_publish_gets_lock_error_not_a_hang(self):
        """Review schedule: "A SIGTERM handler that calls release while main is inside release or
        publish blocks forever, with the lock fd still open"."""
        owner = self.owned()
        seen = []

        def handler(signum, frame):
            try:
                seen.append(self.hl.release(owner))
            except self.hl.LockError as e:
                seen.append("LockError: %s" % e)
        previous = signal.signal(signal.SIGUSR1, handler)
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        real = self.hl._write_record

        def signalled(o, rec):
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(3):
                time.sleep(0.01)
            return real(o, rec)
        signal.alarm(10)
        self.addCleanup(signal.alarm, 0)
        with mock.patch.object(self.hl, "_write_record", signalled):
            got = self.hl.publish(owner, "routine")
        self.assertEqual(seen, ["LockError: re-entrant"], "refused at once, never blocked")
        self.assertTrue(got.durable and owner.published and not owner.released, "the lock stays consistent")
        self.assertEqual(self.hl.release(owner), self.hl.Released(True), "and releases normally afterwards")

    def test_release_after_the_lock_file_was_replaced_leaves_the_record(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        record = (self.ldir() / "holder").read_bytes()
        (self.ldir() / "fresh").write_text("")
        os.replace(self.ldir() / "fresh", self.ldir() / "lock")
        self.assertEqual(self.hl.release(owner), self.hl.Released(False))
        self.assertEqual((self.ldir() / "holder").read_bytes(), record, "removed only through the lock it holds")

    def test_release_leaves_a_record_naming_this_pid_but_another_lock(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        rec = self.hl.read_holder(str(self.root)).record
        other = json.dumps(dict(rec, lock={"dev": rec["lock"]["dev"], "ino": rec["lock"]["ino"] + 1})).encode()
        (self.ldir() / "holder").write_bytes(other)
        self.assertEqual(self.hl.release(owner), self.hl.Released(False))
        self.assertEqual((self.ldir() / "holder").read_bytes(), other)

    def test_the_legacy_read_is_through_the_held_root_not_the_path(self):
        self.root.mkdir()
        pid, _ = self.proc()
        (self.root / "state").mkdir()
        (self.root / "state" / "connector.pid").write_text("%d\n" % pid)
        real = self.hl._read_prior

        def replace_then_read(d):
            os.rename(self.root, self.tmp / "moved")
            self.root.mkdir()
            return real(d)
        with mock.patch.object(self.hl, "_read_prior", replace_then_read):
            r = self.acquire()
        self.assertIsInstance(r, self.hl.LegacyRunning, "the held root's pidfile, not the new path's empty dir")
        self.assertEqual(r.pid, pid)

    def race_two_releases_inside_the_critical_section(self, owner):
        """One release paused inside the critical section, a second release started meanwhile: the
        second must wait for the first and then refuse, never run alongside it."""
        entered, go, first = threading.Event(), threading.Event(), []
        real = self.hl._read_prior

        def slow(d):
            if threading.current_thread() is first[0]:
                entered.set()
                go.wait(10)
            return real(d)
        results = []

        def call():
            try:
                results.append(repr(self.hl.release(owner)))
            except BaseException as e:
                results.append("%s: %s" % (type(e).__name__, e))
        with mock.patch.object(self.hl, "_read_prior", slow):
            a = threading.Thread(target=call)
            first.append(a)
            a.start()
            self.assertTrue(entered.wait(10))
            b = threading.Thread(target=call)
            b.start()
            b.join(0.5)
            go.set()
            a.join(10)
            b.join(10)
        self.assertEqual(sorted(results), ["LockError: released", "Released(record_removed=True)"])

    def test_racing_releases_inside_the_critical_section_never_both_proceed(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.race_two_releases_inside_the_critical_section(owner)

    def test_a_reused_pid_in_a_leftover_legacy_file_is_ignored(self):
        pid, _ = self.proc()
        p = self.root / "state" / "connector.pid"
        p.parent.mkdir(parents=True)
        p.write_text("%d\n" % pid)
        os.utime(p, (time.time() - 100, time.time() - 100))
        self.hl.release(self.owned())
        os.utime(p, (time.time() + 100, time.time() + 100))
        self.assertIsInstance(self.acquire(), self.hl.LegacyRunning, "a pid started before the file: refused")
        os.utime(p, (time.time() - 100, time.time() - 100))
        with mock.patch.object(self.hl, "start_time", return_value=None):
            self.assertIsInstance(self.acquire(), self.hl.LegacyRunning, "an unreadable start time: refused")

    def test_a_uid_with_no_passwd_entry_is_a_lock_error(self):
        self.real_home()
        with mock.patch.object(self.hl.pwd, "getpwuid", side_effect=KeyError("getpwuid(): uid not found")):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.lock_dir(str(self.tmp))
        self.assertIn("no passwd entry", str(e.exception))

    # ── the re-review at ceb81d5 ──

    def test_a_copied_or_rebuilt_owner_can_neither_release_nor_publish(self):
        """Review schedule: "alias = dataclasses.replace(old); release(old); new = acquire(...);
        publish(new, 'new'); release(alias)" removed the new holder and closed new's fds."""
        owner = self.owned()
        self.hl.publish(owner, "routine")
        for make in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(make=make.__name__), self.assertRaises(self.hl.LockError):
                make(owner)
        alias = dataclasses.replace(owner)
        record = (self.ldir() / "holder").read_bytes()
        for call in (lambda: self.hl.release(alias), lambda: self.hl.publish(alias, "alias")):
            with self.assertRaises(self.hl.LockError) as e:
                call()
            self.assertEqual(str(e.exception), "released")
        self.assertEqual((self.ldir() / "holder").read_bytes(), record, "touches nothing")
        os.fstat(owner.fd)
        os.fstat(owner.root_fd)
        self.assertFalse(self.lock_free(), "the lock stays held")

    def test_a_stale_owner_cannot_release_its_successor_even_on_reused_fd_numbers(self):
        old = self.owned()
        alias = dataclasses.replace(old)
        self.hl.release(old)
        new = self.owned()
        for theirs, mine in ((new.fd, old.fd), (new.root_fd, old.root_fd)):
            if theirs != mine and mine not in (new.fd, new.root_fd):
                os.dup2(theirs, mine)
                self.addCleanup(self.close_quietly, mine)
        self.hl.publish(new, "new")
        record = (self.ldir() / "holder").read_bytes()
        for stale in (old, alias):
            with self.assertRaises(self.hl.LockError):
                self.hl.release(stale)
        self.assertEqual((self.ldir() / "holder").read_bytes(), record, "the successor's record is intact")
        os.fstat(new.fd)
        os.fstat(new.root_fd)
        self.assertIs(self.hl._OWNER, new)
        self.assertFalse(new.released)

    def test_a_component_created_concurrently_is_opened_not_refused(self):
        real = os.mkdir

        def someone_else_first(path, *a, **k):
            real(path, *a, **k)
            raise FileExistsError(17, "File exists", os.fspath(path))
        with mock.patch.object(self.hl.os, "mkdir", someone_else_first):
            self.hl.release(self.owned())

    def close_quietly(self, fd):
        with contextlib.suppress(OSError):
            os.close(fd)

    def test_a_component_swapped_for_a_symlink_into_the_root_while_the_tree_is_built_is_refused(self):
        """Review schedule: "Switching the home .local symlink from an outside directory to a
        descendant of the work root after _within returns but before makedirs" made both copies
        create the lock tree under the root."""
        root = self.tmp / "r"
        (root / "descendant").mkdir(parents=True)
        home = self.tmp / "h"
        (home / ".local").mkdir(parents=True)
        real_open, swapped = os.open, []

        def swap_then_open(path, *a, **k):
            if os.fspath(path) == ".local" and k.get("dir_fd") is not None and not swapped:
                swapped.append(True)
                os.rename(home / ".local", home / "outside")
                os.symlink(root / "descendant", home / ".local")
            return real_open(path, *a, **k)
        with mock.patch.object(self.hl, "_home", lambda: str(home)), \
                mock.patch.object(self.hl.os, "open", swap_then_open):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(root)
        self.assertEqual(swapped, [True])
        self.assertEqual(str(e.exception), "work root contains the lock directory")
        self.assertEqual(os.listdir(root / "descendant"), [], "nothing created under the root")

    # ── the code review at a132917 ──

    def test_a_same_inode_reopen_dup2d_onto_the_lock_fd_makes_publish_and_release_refuse(self):
        """Review schedule: "Reopening owner.lock_path and dup2-ing it onto owner.fd preserves that
        inode but closes the locked description. A subprocess then acquires and publishes successor;
        the old canonical publish(owner, "stale") still returns Published and overwrites that record."
        Out of the threat model to prevent; the fresh-open check makes the stale owner refuse."""
        owner = self.owned()
        fresh = os.open(owner.lock_path, os.O_RDWR)
        os.dup2(fresh, owner.fd)
        os.close(fresh)
        self.assertEqual(os.fstat(owner.fd).st_ino, owner.lock_id[1], "same inode: _held_fds still passes")
        code = ("r = hl.acquire(%r, install_id='i')\nprint(type(r).__name__, flush=True)\n"
                "hl.publish(r.owner, 'successor')\nprint('published', flush=True)\nsys.stdin.read()\n"
                "hl.release(r.owner)\n" % str(self.root))
        child = self.run_py(code)
        self.addCleanup(lambda: (child.stdin.close(), child.wait(WAIT), child.stdout.close(), child.stderr.close()))
        self.assertEqual((read_line(child.stdout), read_line(child.stdout)), ("Acquired", "published"))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "stale")
        self.assertEqual(str(e.exception), "lock lost")
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.release(owner)
        self.assertEqual(str(e.exception), "lock lost")
        self.assertEqual(self.hl.read_holder(str(self.root)).record["adapter"], "successor", "not overwritten")
        child.stdin.close()
        child.wait(WAIT)
        owner.released, self.hl._OWNER = True, None
        for fd in (owner.fd, owner.dir_fd, owner.root_fd):
            os.close(fd)

    def test_a_lock_lost_with_nobody_else_holding_is_refused_too(self):
        owner = self.owned()
        fresh = os.open(owner.lock_path, os.O_RDWR)
        os.dup2(fresh, owner.fd)
        os.close(fresh)
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "stale")
        self.assertEqual(str(e.exception), "lock lost")
        self.assertTrue(self.lock_free(), "the check took nothing")
        owner.released, self.hl._OWNER = True, None
        for fd in (owner.fd, owner.dir_fd, owner.root_fd):
            os.close(fd)

    def test_a_checked_directory_renamed_under_the_root_before_mkdir_leaves_nothing_there(self):
        """Review schedule: "ancestry is checked before the following mkdir; renaming that
        already-checked directory under the work root at the seam made acquire refuse only after
        creating root/descendant/state"."""
        root = self.tmp / "r"
        root.mkdir()
        home = self.tmp / "h"
        (home / ".local").mkdir(parents=True)
        real_mkdir, moved = os.mkdir, []

        def move_then_mkdir(path, *a, **k):
            if os.fspath(path) == "state" and k.get("dir_fd") is not None and not moved:
                moved.append(True)
                os.rename(home / ".local", root / "descendant")
            return real_mkdir(path, *a, **k)
        with mock.patch.object(self.hl, "_home", lambda: str(home)), \
                mock.patch.object(self.hl.os, "mkdir", move_then_mkdir):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(root)
        self.assertEqual(moved, [True])
        self.assertEqual(str(e.exception), "work root contains the lock directory")
        self.assertEqual(os.listdir(root / "descendant"), [], "the component it made is removed")

    def test_the_lock_dir_moved_under_the_root_before_the_lock_open_leaves_no_lock_there(self):
        """Review schedule: "Moving the final checked directory before the lock open likewise left
        root/descendant/lock before LockError"."""
        root = self.tmp / "r"
        root.mkdir()
        directory = self.ldir(root)
        real_open, moved = os.open, []

        def move_then_open(path, *a, **k):
            if os.fspath(path) == "lock" and k.get("dir_fd") is not None and not moved:
                moved.append(True)
                os.rename(directory, root / "descendant")
            return real_open(path, *a, **k)
        with mock.patch.object(self.hl.os, "open", move_then_open):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(root)
        self.assertEqual(moved, [True])
        self.assertEqual(str(e.exception), "work root contains the lock directory")
        self.assertEqual(os.listdir(root / "descendant"), [], "no lock file left under the root")

    def test_a_lock_dir_moved_aside_and_symlinked_to_the_root_gets_no_write_through_the_path(self):
        """Review schedule: "Renaming the lock directory aside and symlinking its old path to the work
        root made both copies return Published(durable=True), write work_root/holder ... It also
        deleted a pre-existing work_root/.holder.user-data.tmp during cleanup"."""
        owner = self.owned()
        victim = self.root / ".holder.user-data.tmp"
        victim.write_bytes(b"user data")
        moved = self.tmp / "moved-lock-dir"
        os.rename(owner.directory, moved)
        os.symlink(self.root, owner.directory)
        self.addCleanup(lambda: os.path.islink(owner.directory) and os.unlink(owner.directory))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "replaced")
        self.assertEqual(sorted(os.listdir(self.root)), [".holder.user-data.tmp"], "no holder in the root")
        self.assertEqual(victim.read_bytes(), b"user data")
        self.assertEqual(os.listdir(moved), ["lock"])
        self.assertEqual(self.hl.release(owner), self.hl.Released(False))
        self.assertEqual(sorted(os.listdir(self.root)), [".holder.user-data.tmp"])

    def test_every_entry_point_checks_the_platform_before_touching_the_filesystem(self):
        """Review: "with pwd=None, a missing root yields FileNotFoundError / Missing, not LockError"."""
        owner = self.owned()
        missing = str(self.tmp / "absent" / "root")
        touched = []

        def spy(name):
            real = getattr(os, name)
            return lambda *a, **k: (touched.append(name), real(*a, **k))[1]
        calls = [("lock_dir", lambda: self.hl.lock_dir(missing)),
                 ("read_holder", lambda: self.hl.read_holder(missing)),
                 ("status", lambda: self.hl.status(missing)),
                 ("acquire", lambda: self.hl.acquire(missing, install_id="i")),
                 ("acquire with a timeout", lambda: self.hl.acquire(missing, install_id="i", timeout=1)),
                 ("wait", lambda: self.hl.wait(missing, install_id="i", deadline=time.monotonic() + 1)),
                 ("publish", lambda: self.hl.publish(owner, "routine")),
                 ("occupancy", lambda: self.hl.occupancy(missing)),
                 ("attach_consumer", lambda: self.hl.attach_consumer(owner, b"", pid=1, start="s")),
                 ("tombstone_consumer", lambda: self.hl.tombstone_consumer(missing, b"", by="t", reason="r")),
                 ("release", lambda: self.hl.release(owner))]
        for name, call in calls:
            with self.subTest(entry=name):
                touched.clear()
                with mock.patch.object(self.hl, "pwd", None), \
                        contextlib.ExitStack() as stack:
                    for fn in ("stat", "lstat", "open", "mkdir", "listdir", "scandir"):
                        stack.enter_context(mock.patch.object(self.hl.os, fn, spy(fn)))
                    with self.assertRaises(self.hl.LockError) as e:
                        call()
                self.assertEqual(str(e.exception), self.hl.UNSUPPORTED)
                self.assertEqual(touched, [], "nothing touched before the platform check")
        self.assertFalse((self.tmp / "absent").exists())
        self.hl.release(owner)

    def test_read_line_keeps_no_bytes_for_a_later_pipe_that_reuses_the_fd_number(self):
        """Review schedule: "After reading old-1 and closing that pipe with old-2 buffered, a new child
        reusing the fd returned old-2 instead of new-1"."""
        r, w = os.pipe()
        old = os.fdopen(r, "rb", buffering=0)
        os.write(w, b"old-1\nold-2\n")
        os.close(w)
        self.assertEqual(read_line(old), "old-1")
        number = old.fileno()
        old.close()
        r2, w2 = os.pipe()
        self.assertEqual(r2, number, "the fd number is reused")
        new = os.fdopen(r2, "rb", buffering=0)
        self.addCleanup(new.close)
        os.write(w2, b"new-1\n")
        os.close(w2)
        self.assertEqual(read_line(new), "new-1")

    # ── the code review at 53214f2 ──

    def test_a_waiter_never_writes_or_removes_anything_under_the_work_root_even_when_the_lock_dir_is_redirected(self):
        """Review schedule: "Pre-create work_root/waiting.<pid> with USER-DATA. Immediately after
        wait() closes the fd returned by _prepare, rename the verified lock directory aside and replace
        its old path with a symlink to work_root" -- it replaced, then deleted, the user's file."""
        directory = self.ldir()
        (self.root / ("waiting.%d" % os.getpid())).write_text("USER-DATA")
        (self.root / "notes").mkdir()
        (self.root / "notes" / "keep.txt").write_bytes(b"other user bytes")
        before = snapshot(str(self.root))
        moved = self.tmp / "moved-lock-dir"
        real_prepare, real_check, seen = self.hl._prepare, self.hl._dir_unchanged, []

        def prepare_then_redirect(*a, **k):
            got = real_prepare(*a, **k)
            if not seen:
                seen.append("redirected")
                os.rename(directory, moved)
                os.symlink(self.root, directory)
            return got

        def check(*a, **k):
            seen.append(sorted(n for n in os.listdir(moved) if n.startswith("waiting.")))
            return real_check(*a, **k)
        self.addCleanup(lambda: os.path.islink(directory) and os.unlink(directory))
        with mock.patch.object(self.hl, "_prepare", prepare_then_redirect), \
                mock.patch.object(self.hl, "_dir_unchanged", check):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 2)
        self.assertEqual(str(e.exception), "replaced", "refused before any attempt")
        self.assertEqual(seen, ["redirected", ["waiting.%d" % os.getpid()]], "the waiter landed in the original")
        self.assertEqual([n for n in os.listdir(moved) if "waiting" in n], [], "and was removed from it")
        self.assertEqual(snapshot(str(self.root)), before, "the work root is byte-for-byte untouched")
        self.assertIsNone(self.hl._WAITING)

    def test_a_failed_waiter_write_leaves_no_temp_and_is_raised(self):
        with mock.patch.object(self.hl.os, "replace", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 1)
        self.assertEqual([n for n in os.listdir(self.ldir()) if "waiting" in n], [], "the temp is removed")
        self.assertIsNone(self.hl._WAITING)

    def test_a_component_renamed_under_the_root_before_a_missing_part_is_opened_creates_nothing(self):
        root = self.tmp / "r"
        root.mkdir()
        home = self.tmp / "h"
        (home / ".local").mkdir(parents=True)
        real_open, moved = os.open, []

        def move_then_open(path, *a, **k):
            if os.fspath(path) == "state" and k.get("dir_fd") is not None and not moved:
                moved.append(True)
                os.rename(home / ".local", root / "descendant")
            return real_open(path, *a, **k)
        with mock.patch.object(self.hl, "_home", lambda: str(home)), \
                mock.patch.object(self.hl.os, "open", move_then_open):
            with self.assertRaises(self.hl.LockError) as e:
                self.acquire(root)
        self.assertEqual((moved, str(e.exception)), ([True], "work root contains the lock directory"))
        self.assertEqual(os.listdir(root / "descendant"), [], "refused before mkdir")

    def test_a_lock_file_created_by_a_racer_between_the_two_opens_is_used(self):
        directory = self.ldir()
        real_open = os.open

        def racer_first(path, flags, *a, **k):
            if os.fspath(path) == "lock" and flags & os.O_EXCL:
                os.close(real_open(str(directory / "lock"), os.O_RDWR | os.O_CREAT, 0o600))
            return real_open(path, flags, *a, **k)
        with mock.patch.object(self.hl.os, "open", racer_first):
            owner = self.owned()
        self.assertEqual(os.stat(directory / "lock").st_ino, owner.lock_id[1])
        self.hl.release(owner)

    def test_the_identity_helpers_answer_false_on_unusable_descriptors(self):
        self.assertFalse(self.hl._same_at(-1, -1, "lock"))
        self.assertFalse(self.hl._dir_unchanged(str(self.tmp / "absent"), -1))

    def test_the_lock_held_check_refuses_an_unopenable_or_swapped_lock_file(self):
        owner = self.owned()
        real_open = os.open

        def fail_plain_lock_open(path, flags, *a, **k):
            if os.fspath(path) == "lock" and not flags & os.O_CREAT:
                raise OSError(13, "Permission denied")
            return real_open(path, flags, *a, **k)
        with mock.patch.object(self.hl.os, "open", fail_plain_lock_open):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.publish(owner, "routine")
        self.assertTrue(str(e.exception).startswith("lock lost: "), e.exception)

        def swap_then_open(path, flags, *a, **k):
            if os.fspath(path) == "lock" and not flags & os.O_CREAT:
                fresh = Path(owner.directory) / "fresh"
                fresh.write_bytes(b"")
                os.replace(fresh, Path(owner.directory) / "lock")
            return real_open(path, flags, *a, **k)
        with mock.patch.object(self.hl, "_same_at", lambda *a: True), \
                mock.patch.object(self.hl.os, "open", swap_then_open):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.publish(owner, "routine")
        self.assertEqual(str(e.exception), "replaced")
        owner.released, self.hl._OWNER = True, None
        for fd in (owner.fd, owner.dir_fd, owner.root_fd):
            os.close(fd)

    def test_every_waiter_file_operation_in_wait_goes_through_the_lock_dir_fd(self):
        code = ("r = hl.acquire(%r, install_id='i')\nprint(type(r).__name__, flush=True)\nsys.stdin.read()\n"
                "hl.release(r.owner)\n" % str(self.root))
        child = self.run_py(code)
        self.addCleanup(lambda: (child.stdin.close(), child.wait(WAIT), child.stdout.close(), child.stderr.close()))
        self.assertEqual(read_line(child.stdout), "Acquired")
        calls = []

        def spy(name):
            real = getattr(os, name)
            return lambda *a, **k: (calls.append((name, a, k)), real(*a, **k))[1]
        with contextlib.ExitStack() as stack:
            for fn in ("open", "replace", "unlink", "rename", "remove"):
                stack.enter_context(mock.patch.object(self.hl.os, fn, spy(fn)))
            got = self.hl.wait(str(self.root), install_id="i", deadline=time.monotonic() + 0.3, poll=0.05)
        self.assertIsInstance(got, self.hl.TimedOut)
        waiter = [(n, a, k) for n, a, k in calls if a and "waiting." in str(a[0])]
        self.assertEqual(sorted({n for n, _, _ in waiter}), ["open", "replace", "unlink"], waiter)
        for n, a, k in waiter:
            self.assertNotIn(os.sep, str(a[0]), "a bare name: %r" % ((n, a, k),))
            keys = ("src_dir_fd", "dst_dir_fd") if n == "replace" else ("dir_fd",)
            self.assertTrue(all(isinstance(k.get(key), int) for key in keys), (n, a, k))
        self.assertEqual([x for x in os.listdir(self.ldir()) if "waiting." in x], [])

    def test_no_function_acting_after_the_lock_dir_is_opened_does_path_based_io(self):
        """Grep-pin: in every function that runs with the lock directory held, each os call that opens,
        lists, renames or removes takes a descriptor (dir_fd=, src/dst_dir_fd=, or an fd argument),
        and nothing calls the builtin open."""
        tree = ast.parse(Path(self.hl.__file__).read_text())
        held = {"_attempt", "_lock_still_held", "_write_record", "_write_record_at", "_raw_record", "publish",
                "attach_consumer", "release", "_write_waiting", "wait"}
        io_calls = {"open", "unlink", "remove", "replace", "rename", "mkdir", "rmdir", "listdir", "stat", "lstat"}
        found = set()
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in held):
            found.add(fn.name)
            for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
                f = call.func
                if isinstance(f, ast.Name):
                    self.assertNotEqual(f.id, "open", "%s calls the builtin open" % fn.name)
                if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os"
                        and f.attr in io_calls):
                    continue
                keywords = {k.arg for k in call.keywords}
                first = ast.unparse(call.args[0]) if call.args else ""
                self.assertTrue(keywords & {"dir_fd", "src_dir_fd"} or first.endswith("fd"),
                                "%s: os.%s(%s) is path-based" % (fn.name, f.attr, first))
        self.assertEqual(found, held)

    # ── schema 2: the consumer, the two CAS writers, occupancy ──

    def test_a_schema_2_record_with_a_live_or_unobserved_consumer_is_held_and_a_dead_one_is_free(self):
        hpid, hstart = self.dead()
        cpid, cstart = self.proc()
        self.put(self.record2(hpid, hstart, attached=(cpid, cstart)))
        r = self.acquire()
        self.assertEqual((type(r).__name__, r.reason), ("Held", "live consumer"), "a dead holder, a live consumer")
        self.put(self.record2(hpid, hstart, attached=(cpid, cstart), host="another-host"))
        self.assertEqual(self.acquire().reason, "unobserved")
        self.put(self.record2(hpid, hstart, attached=(cpid, cstart)))
        with mock.patch.object(self.hl, "verify_lineage",
                               lambda pid, start, host=None: False if pid == hpid else None):
            self.assertEqual(self.acquire().reason, "unobserved consumer")
        dpid, dstart = self.dead()
        self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        owner = self.owned()
        self.hl.publish(owner, "routine")
        rec = self.hl.read_holder(str(self.root)).record
        self.assertEqual((rec["owner_pid"], rec["consumer"]), (os.getpid(), None), "a dead pair is overwritten")
        self.hl.release(owner)
        for bad in (dict(consumer={"pid": 5}), dict(consumer={"pid": True, "start": "s"}),
                    dict(tombstone={"by": "t", "at": 1, "reason": "r"}), dict(owner_pid=0), dict(owner_start="")):
            with self.subTest(bad=bad):
                self.put(self.record2(hpid, hstart, **bad))
                self.assertIsInstance(self.hl.read_holder(str(self.root)), self.hl.Invalid)

    def test_each_unverifiable_read_is_a_refusal_or_unobserved_never_free(self):
        self.assertIsNone(self.hl.parent_pid("not-a-pid"), "an unusable pid has no parent")
        d = self.ldir()
        d.mkdir(parents=True, exist_ok=True)
        dfd = os.open(str(d), os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        self.assertIsNone(self.hl._raw_record(dfd), "no record is None")
        os.symlink("elsewhere", str(d / "holder"))
        with self.assertRaises(self.hl.LockError):
            self.hl._raw_record(dfd)
        os.unlink(str(d / "holder"))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl._own_record(object(), None)
        self.assertEqual(str(e.exception), "not this owner's record")
        with mock.patch.object(self.hl, "verify_lineage", return_value=None):
            self.assertFalse(self.hl._proven_dead({"owner_pid": 1, "owner_start": "s", "host": "h",
                                                   "consumer": {"pid": 2, "start": "t"}}))
        with mock.patch.object(self.hl, "lock_dir", side_effect=PermissionError(13, "denied")):
            self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved")
        os.symlink("lock", str(d / "lock"))                      # a loop: the lock cannot be opened
        self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved")
        os.unlink(str(d / "lock"))
        (d / "lock").touch()
        with mock.patch.object(self.hl.file_lock, "try_lock_fd", side_effect=OSError(5, "io")):
            self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved")
        with mock.patch.object(self.hl, "_read_prior", side_effect=ValueError("bad")):
            self.assertEqual(self.hl.occupancy(str(self.root)), self.hl._occupant(self.hl.Invalid("x", b""))[0],
                             "an undecodable record reads as an invalid one")

    def test_attach_refuses_a_replaced_lock_and_a_record_it_cannot_validate(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        published = (self.ldir() / "holder").read_bytes()
        cpid, cstart = self.proc()
        with mock.patch.object(self.hl, "_same_at", return_value=False), self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, published, pid=cpid, start=cstart)
        self.assertEqual(str(e.exception), "replaced")
        real = self.hl._record_problem
        bad = mock.patch.object(self.hl, "_record_problem", lambda rec: "bad" if rec.get("consumer") else real(rec))
        with bad, self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, published, pid=cpid, start=cstart)
        self.assertEqual(str(e.exception), "invalid record: bad")
        self.assertEqual((self.ldir() / "holder").read_bytes(), published, "nothing written")
        self.end(cpid)
        self.hl.release(owner)

    def test_tombstone_refuses_a_missing_lock_a_schema_1_record_and_an_invalid_result(self):
        hpid, hstart = self.dead()
        dpid, dstart = self.dead()
        dead = self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="r")
        self.assertTrue(str(e.exception).startswith("no lock to take"), e.exception)
        (self.ldir() / "lock").touch()
        old = self.put(self.record(hpid, hstart))
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.tombstone_consumer(str(self.root), old, by="launcher", reason="r")
        self.assertEqual(str(e.exception), "not a schema-2 record")
        dead = self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        real = self.hl._record_problem
        bad = mock.patch.object(self.hl, "_record_problem", lambda rec: "bad" if rec.get("tombstone") else real(rec))
        with mock.patch.object(self.hl, "_proven_dead", return_value=True), bad, self.assertRaises(self.hl.LockError) as e:
            self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="r")
        self.assertEqual(str(e.exception), "invalid record: bad")
        self.assertEqual((self.ldir() / "holder").read_bytes(), dead, "nothing written")

    def test_attach_consumer_is_a_cas_that_moves_the_fencing_pid_to_the_consumer(self):
        owner = self.owned()
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, b"", pid=1, start="s")
        self.assertEqual(str(e.exception), "not published")
        self.hl.publish(owner, "routine")
        published = (self.ldir() / "holder").read_bytes()
        cpid, cstart = self.proc()
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, published + b" ", pid=cpid, start=cstart)
        self.assertEqual(str(e.exception), "record changed")
        self.assertEqual((self.ldir() / "holder").read_bytes(), published, "nothing written on a changed record")
        got = self.hl.attach_consumer(owner, published, pid=cpid, start=cstart)
        self.assertIsInstance(got, self.hl.Published)
        rec = self.hl.read_holder(str(self.root)).record
        self.assertEqual(got.record, rec)
        self.assertEqual((rec["pid"], rec["start"], rec["consumer"]), (cpid, cstart, {"pid": cpid, "start": cstart}),
                         "the top-level pid now names the consumer")
        self.assertEqual((rec["owner_pid"], rec["owner_start"]), (os.getpid(), owner.start))
        self.assertEqual(self.hl._pids_in((self.ldir() / "holder").read_bytes()), [(cpid, cstart)],
                         "a schema-1 reader scanning the raw bytes finds the consumer")
        attached = (self.ldir() / "holder").read_bytes()
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, attached, pid=cpid, start=cstart)
        self.assertEqual(str(e.exception), "consumer attached")
        self.assertEqual((self.ldir() / "holder").read_bytes(), attached)
        self.assertEqual(self.hl.status(str(self.root))["consumer_lineage"], True)
        self.end(cpid)
        self.assertEqual(self.hl.release(owner), self.hl.Released(True), "release validates the owner fields")
        owner = self.owned()
        self.hl.publish(owner, "routine")
        self.hl.release(owner)
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.attach_consumer(owner, b"", pid=cpid, start=cstart)
        self.assertEqual(str(e.exception), "released")

    def test_release_refuses_while_the_consumer_is_alive_or_unobserved_and_keeps_the_flock(self):
        owner = self.owned()
        self.hl.publish(owner, "claude-local")
        cpid, cstart = self.proc()
        self.hl.attach_consumer(owner, (self.ldir() / "holder").read_bytes(), pid=cpid, start=cstart)
        attached = (self.ldir() / "holder").read_bytes()
        for seen in (True, None):
            with mock.patch.object(self.hl, "verify_lineage", side_effect=lambda pid, start, host=None:
                                   seen if pid == cpid else True), self.assertRaises(self.hl.LockError) as e:
                self.hl.release(owner)
            self.assertEqual(str(e.exception), "consumer alive")
            self.assertEqual((self.ldir() / "holder").read_bytes(), attached, "nothing removed")
            self.assertFalse(owner.released)
            self.assertFalse(self.lock_free(), "the flock is kept")
        self.end(cpid)
        self.assertEqual(self.hl.release(owner), self.hl.Released(True), "a dead consumer: the record goes")
        code = ("import os, subprocess, time\nroot = %r\n"
                "o = hl.acquire(root, install_id=hl.install_id()).owner; hl.publish(o, 'claude-local')\n"
                "c = subprocess.Popen(['sleep', '300'], start_new_session=True, stdin=-3, stdout=-3, stderr=-3)\n"
                "st = None\nwhile not st: st = hl.start_time(c.pid); time.sleep(0.02)\n"
                "hl.attach_consumer(o, open(os.path.join(hl.lock_dir(root), 'holder'), 'rb').read(), pid=c.pid, start=st)\n"
                "try:\n    hl.release(o); print('released', c.pid)\n"
                "except hl.LockError as e:\n    print('refused', c.pid, e)\n") % str(self.root)
        out, err = self.run_py(code).communicate(timeout=WAIT)
        word, pid = out.split()[:2]
        self.addCleanup(self.kill_quiet, int(pid))
        self.assertEqual(word, "refused", out + err)
        self.assertEqual(self.hl.occupancy(str(self.root)), "held", "the holder exited; its live consumer still fences")
        os.kill(int(pid), signal.SIGKILL)
        deadline = time.monotonic() + WAIT
        while self.hl.occupancy(str(self.root)) != "free" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "the consumer gone, nobody fences")

    def test_release_validates_the_owner_fields_of_a_schema_2_record(self):
        owner = self.owned()
        self.hl.publish(owner, "routine")
        dpid, dstart = self.dead()
        lock = {"dev": owner.lock_id[0], "ino": owner.lock_id[1]}
        theirs = self.put(self.record2(dpid, dstart, attached=(os.getpid(), owner.start), lock=lock))
        self.assertEqual(self.hl.release(owner), self.hl.Released(False),
                         "a record whose consumer is this pid but whose owner is another is left")
        self.assertEqual((self.ldir() / "holder").read_bytes(), theirs)
        self.assertEqual(self.acquire().reason, "live consumer", "and that consumer (this process) still fences")
        os.unlink(self.ldir() / "holder")
        owner = self.owned()
        cpid, cstart = self.dead()
        lock = {"dev": owner.lock_id[0], "ino": owner.lock_id[1]}
        self.put(self.record2(os.getpid(), owner.start, attached=(cpid, cstart), lock=lock))
        self.assertEqual(self.hl.release(owner), self.hl.Released(True),
                         "a record whose owner fields are this process is removed whatever the fencing pid")
        self.assertFalse((self.ldir() / "holder").exists())

    def test_tombstone_consumer_refuses_a_lock_file_or_directory_replaced_before_the_write(self):
        hpid, hstart = self.dead()
        dpid, dstart = self.dead()
        dead = self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        (self.ldir() / "lock").touch()
        directory = self.ldir()
        for name, swap in (("the lock file", lambda: (os.rename(directory / "lock", directory / "lock.old"),
                                                       (directory / "lock").touch())),
                           ("the directory", lambda: (os.rename(directory, str(directory) + ".aside"),
                                                      os.mkdir(directory)))):
            def swap_after_the_proof(rec, swap=swap):
                swap()
                return True
            with mock.patch.object(self.hl, "_proven_dead", swap_after_the_proof), \
                    self.assertRaises(self.hl.LockError) as e:
                self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="proved gone")
            self.assertEqual(str(e.exception), "replaced", name)
            held = Path(str(directory) + ".aside") if name == "the directory" else directory
            self.assertEqual((held / "holder").read_bytes(), dead, "%s: nothing written" % name)
            if name == "the lock file":
                os.rename(directory / "lock.old", directory / "lock")

    def test_attach_consumer_names_only_the_holders_own_live_child(self):
        owner = self.owned()
        self.hl.publish(owner, "claude-local")
        published = (self.ldir() / "holder").read_bytes()
        stranger = os.getppid()
        dpid, dstart = self.dead()
        for pid, start, why in ((stranger, self.hl.start_time(stranger), "another process's child"),
                                (dpid, dstart, "a dead child")):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.attach_consumer(owner, published, pid=pid, start=start)
            self.assertEqual(str(e.exception), "not the holder's live child", why)
            self.assertEqual((self.ldir() / "holder").read_bytes(), published, "%s: nothing written" % why)
        cpid, cstart = self.proc()
        self.hl.attach_consumer(owner, published, pid=cpid, start=cstart)
        self.end(cpid)

    def test_tombstone_consumer_needs_the_pair_dead_twice_and_never_removes_the_record(self):
        hpid, hstart = self.dead()
        cpid, cstart = self.proc()
        live = self.put(self.record2(hpid, hstart, attached=(cpid, cstart)))
        (self.ldir() / "lock").touch()
        slept = []
        with mock.patch.object(self.hl.time, "sleep", slept.append):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.tombstone_consumer(str(self.root), live, by="launcher", reason="proved gone")
            self.assertEqual(str(e.exception), "not proven dead")
            self.assertEqual((self.ldir() / "holder").read_bytes(), live, "a live consumer: nothing written")
            dpid, dstart = self.dead()
            dead = self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.tombstone_consumer(str(self.root), dead + b" ", by="launcher", reason="proved gone")
            self.assertEqual(str(e.exception), "record changed")
            with self.flock_elsewhere():
                with self.assertRaises(self.hl.LockError) as e:
                    self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="proved gone")
                self.assertEqual(str(e.exception), "held")
            self.assertEqual((self.ldir() / "holder").read_bytes(), dead)
            checks = []
            real = self.hl.verify_lineage
            with mock.patch.object(self.hl, "verify_lineage",
                                   lambda pid, start, host=None: (checks.append(pid), real(pid, start, host))[1]):
                got = self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="proved gone")
        self.assertEqual([x for x in slept if x == self.hl.TOMBSTONE_RECHECK_S], [self.hl.TOMBSTONE_RECHECK_S],
                         "verified twice, one pause apart (subprocess's own waits aside)")
        self.assertEqual(checks, [hpid, dpid, hpid, dpid])
        self.assertIsInstance(got, self.hl.Published)
        rec = self.hl.read_holder(str(self.root)).record
        self.assertEqual(got.record, rec)
        self.assertEqual((rec["pid"], rec["start"], rec["consumer"]), (hpid, hstart, None), "fenced by the dead holder")
        tombstoned = (self.ldir() / "holder").read_bytes()
        dead = self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))

        def swap_during_the_pause(seconds):
            if seconds == self.hl.TOMBSTONE_RECHECK_S:
                self.put(self.record2(hpid, hstart, attached=(dpid, dstart), adapter="other"))
        with mock.patch.object(self.hl.time, "sleep", swap_during_the_pause):
            with self.assertRaises(self.hl.LockError) as e:
                self.hl.tombstone_consumer(str(self.root), dead, by="launcher", reason="proved gone")
        self.assertEqual(str(e.exception), "record changed", "the compare is repeated after the pause")
        self.assertEqual(json.loads((self.ldir() / "holder").read_bytes())["adapter"], "other", "nothing written")
        self.put(tombstoned)
        self.assertEqual((rec["tombstone"]["by"], rec["tombstone"]["reason"]), ("launcher", "proved gone"))
        self.assertIsInstance(rec["tombstone"]["at"], float)
        self.assertEqual(sorted(os.listdir(self.ldir())), ["holder", "lock"], "never removed, no temp")
        self.assertTrue(self.lock_free())
        with self.assertRaises(self.hl.LockError) as e:
            self.hl.tombstone_consumer(str(self.root), (self.ldir() / "holder").read_bytes(), by="l", reason="r")
        self.assertEqual(str(e.exception), "no consumer")
        self.assertIsInstance(self.acquire(), self.hl.Acquired, "the tombstoned root is free")

    def test_occupancy_is_the_shared_tri_state_reader_with_the_sticky_rule(self):
        self.assertEqual(self.hl.occupancy(str(self.tmp / "absent")), "free")
        self.root.mkdir()
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "no lock dir yet")
        with self.flock_elsewhere():
            self.assertEqual(self.hl.occupancy(str(self.root)), "held")
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "the probe released its flock")
        hpid, hstart = self.dead()
        cpid, cstart = self.proc()
        self.put(self.record2(hpid, hstart, attached=(cpid, cstart)))
        self.assertEqual(self.hl.occupancy(str(self.root)), "held", "a free flock, a live consumer: sticky")
        self.put(self.record2(hpid, hstart, attached=(cpid, cstart), host="another-host"))
        self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved")
        with mock.patch.object(self.hl, "verify_lineage", lambda pid, start, host=None: False if pid == hpid else None):
            self.put(self.record2(hpid, hstart, attached=(cpid, cstart)))
            self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved", "an unverifiable consumer is never free")
        dpid, dstart = self.dead()
        self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "both dead")
        self.put(self.record2(hpid, hstart, tombstone={"by": "l", "at": 1.0, "reason": "r"}))
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "tombstoned")
        self.put(self.record(cpid, cstart))
        self.assertEqual(self.hl.occupancy(str(self.root)), "held", "a schema-1 live holder")
        self.put(b'{"pid": %d, "torn' % cpid)
        self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved", "a torn record naming a live pid, no start")
        invalid = self.record(cpid, cstart)
        del invalid["adapter"]
        self.put(invalid)
        self.assertEqual(self.hl.occupancy(str(self.root)), "held", "an invalid record naming a live pid")
        self.put(b'{"pid": %d, "torn' % dpid)
        self.assertEqual(self.hl.occupancy(str(self.root)), "free")
        os.unlink(self.ldir() / "holder")
        os.mkdir(self.ldir() / "holder")
        self.assertEqual(self.hl.occupancy(str(self.root)), "unobserved", "unreadable is never free")
        os.rmdir(self.ldir() / "holder")
        self.assertEqual(self.hl.occupancy(str(self.root)), "free", "no record")

    def test_a_schema_1_reader_from_main_refuses_a_live_consumer_and_frees_a_dead_one(self):
        old = self.main_module()
        self.assertEqual(old.SCHEMA, 1, "the rolling pin needs the schema-1 reader")
        owner = self.owned()
        self.hl.publish(owner, "claude-local")
        cpid, cstart = self.proc()
        self.hl.attach_consumer(owner, (self.ldir() / "holder").read_bytes(), pid=cpid, start=cstart)
        attached = json.loads((self.ldir() / "holder").read_bytes())
        with self.assertRaises(self.hl.LockError):
            self.hl.release(owner)
        self.exit_owner(owner)
        hpid, hstart = self.dead()
        self.put(dict(attached, owner_pid=hpid, owner_start=hstart))   # the holder died, its consumer lives on
        self.assertIsInstance(old.read_holder(str(self.root)), old.Invalid, "schema 2 is unsupported there")
        r = old.acquire(str(self.root), install_id="i")
        self.assertEqual((type(r).__name__, r.reason), ("Held", "invalid record naming a live or unobserved pid"))
        dpid, dstart = self.dead()
        self.put(self.record2(hpid, hstart, attached=(dpid, dstart)))
        r = old.acquire(str(self.root), install_id="i")
        self.assertEqual(type(r).__name__, "Acquired", "a dead consumer frees the root for the old reader")
        old.release(r.owner)

    def test_every_case_runs_against_the_source_and_the_vendored_copy(self):
        self.assertIsNot(SRC, VENDORED)
        self.assertEqual(Path(SRC.__file__).read_bytes(), Path(VENDORED.__file__).read_bytes())
        self.assertIs(HolderLockTestVendored.hl, VENDORED)
        cases = {n for n in dir(HolderLockTest) if n.startswith("test_")}
        self.assertEqual(cases, {n for n in dir(HolderLockTestVendored) if n.startswith("test_")})
        self.assertGreaterEqual(len(cases), 40)


class HolderLockTestVendored(HolderLockTest):
    hl = VENDORED
    kind = "vendored"


class WindowsPlatformTest(unittest.TestCase):
    """The real Windows run of §1.7 (the label-gated Windows job); skipped elsewhere."""

    @unittest.skipUnless(os.name == "nt", "real only on Windows")
    def test_on_windows_every_entry_point_raises_lock_error(self):
        for hl in (SRC, VENDORED):
            dummy = hl.Owner(fd=-1, lock_id=(0, 0), pid=os.getpid(), start="s", root="r", directory="d",
                             install_id="i", acquired_at=1.0)
            for call in (lambda: hl.acquire("w", install_id="i"),
                         lambda: hl.wait("w", install_id="i", deadline=time.monotonic() + 1),
                         lambda: hl.publish(dummy, "routine"), lambda: hl.release(dummy),
                         lambda: hl.status("w"), lambda: hl.read_holder("."), lambda: hl.lock_dir(".")):
                with self.assertRaises(hl.LockError):
                    call()


if __name__ == "__main__":
    unittest.main()
