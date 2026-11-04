#!/usr/bin/env python3
"""sparrowd serves its control socket for any workspace path and never crash-loops on its length.

A state dir whose `sparrowd.sock` would exceed the AF_UNIX `sun_path` cap gets a short,
deterministic per-instance path in a private 0700 directory; server and every client resolve it
through `ag2_sparrow.control_socket.control_socket_path`, the only place a socket path is built.
Run: python3 tests/sparrowd-control-socket-path.test.py"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from ag2_sparrow import control_socket as cs  # noqa: E402

SLEEPER = "import time\nwhile True: time.sleep(0.2)\n"


def wait_for(pred, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        # Short roots under /tmp so the fallback itself fits; never the real shared directory.
        self.td = Path(tempfile.mkdtemp(prefix="cs", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.root = self.td / "r"
        self.root.mkdir()
        patcher = mock.patch.object(cs, "SHORT_ROOT", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def long_state(self, tag="a") -> Path:
        state = self.td / ("x" * 120) / tag / "state" / "sparrowd"
        state.mkdir(parents=True)
        self.assertFalse(cs.fits(state / cs.SOCKET_NAME), "fixture must exceed the AF_UNIX limit")
        return state

    def start_daemon(self, state: Path) -> subprocess.Popen:
        fd, name = tempfile.mkstemp(suffix=".py", dir=self.td)
        os.close(fd)
        runner = Path(name)
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(PKG)!r})\n"
            "from ag2_sparrow import control_socket as cs\n"
            f"cs.SHORT_ROOT = {str(self.root)!r}\n"
            "from ag2_sparrow.sparrowd import WorkerSpec, run\n"
            f"raise SystemExit(run([WorkerSpec('w', [sys.executable, '-c', {SLEEPER!r}])], {str(state)!r},"
            " backoff_initial_s=0.2))\n")
        proc = subprocess.Popen([sys.executable, str(runner)], env=hermetic_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc


def ask(path: Path, payload: dict) -> dict:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.settimeout(5)
        c.connect(str(path))
        c.sendall((json.dumps(payload) + "\n").encode())
        return json.loads(c.makefile("r").readline())
    finally:
        c.close()


class LongWorkspace(Base):
    def test_a_state_dir_over_the_limit_starts_and_the_client_connects(self):
        state = self.long_state()
        proc = self.start_daemon(state)
        sock = cs.control_socket_path(state)
        self.assertTrue(cs.fits(sock))
        self.assertEqual(sock.parent, self.root / f"ag2-sparrow-{os.getuid()}")
        self.assertTrue(wait_for(sock.exists), "the short control socket appears")
        self.assertEqual(oct(os.stat(sock.parent).st_mode & 0o777), oct(0o700))
        self.assertTrue(wait_for(lambda: ask(sock, {"op": "status"})["workers"]["w"]["state"] == "running"))
        self.assertTrue(ask(sock, {"op": "stop"})["stopping"])
        out, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(sock.exists(), "socket removed on shutdown")

    def test_the_short_path_is_stable_across_restarts(self):
        state = self.long_state()
        first = cs.control_socket_path(state)
        for _ in range(2):
            proc = self.start_daemon(state)
            self.assertTrue(wait_for(first.exists))
            self.assertEqual(cs.control_socket_path(state), first)
            ask(first, {"op": "stop"})
            self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(cs.control_socket_path(str(state) + "/"), first, "spelling does not move it")

    def test_two_workspaces_get_different_sockets(self):
        a, b = self.long_state("a"), self.long_state("b")
        self.assertNotEqual(cs.control_socket_path(a), cs.control_socket_path(b))
        pa, pb = self.start_daemon(a), self.start_daemon(b)
        sa, sb = cs.control_socket_path(a), cs.control_socket_path(b)
        self.assertTrue(wait_for(lambda: sa.exists() and sb.exists()), "both daemons serve at once")
        for s, p in ((sa, pa), (sb, pb)):
            ask(s, {"op": "stop"})
            self.assertEqual(p.wait(timeout=15), 0)

    def test_a_path_that_fits_keeps_the_natural_location(self):
        state = self.td / "s"
        self.assertEqual(cs.control_socket_path(state, create=True), Path(os.path.realpath(state)) / cs.SOCKET_NAME)
        self.assertFalse((self.root / f"ag2-sparrow-{os.getuid()}").exists(), "no short dir made")


    def test_every_spelling_of_one_state_dir_gets_one_socket(self):
        state = self.long_state()
        link = self.td / "l"
        link.symlink_to(state)
        self.assertEqual(cs.control_socket_path(link), cs.control_socket_path(state))
        short = self.td / "fits"
        short.mkdir()
        # A short alias of a long dir must not pick the natural path the long spelling cannot bind.
        alias = self.td / "z"
        alias.symlink_to(state)
        self.assertNotEqual(cs.control_socket_path(alias).parent, alias)
        self.assertEqual(cs.control_socket_path(short / ".." / "fits"), cs.control_socket_path(short))

    def test_a_client_of_a_missing_short_dir_gets_the_path_without_creating_it(self):
        state = self.long_state()
        self.assertEqual(cs.control_socket_path(state).parent, cs.short_dir())
        self.assertFalse(cs.short_dir().exists())

    def test_a_root_too_long_for_any_socket_is_a_clear_error(self):
        with self.assertRaisesRegex(cs.ControlSocketPathError, "no control socket path fits"):
            cs.control_socket_path(self.long_state(), root=str(self.td / ("y" * 120)))


class InProcessRun(Base):
    """The daemon's fallback legs, in-process so the entry path is measured."""

    def run_until_sigterm(self, state, **patches):
        from ag2_sparrow import sparrowd
        handlers = {}

        def fire():
            if wait_for(lambda: signal.SIGTERM in handlers, 5):
                handlers[signal.SIGTERM](signal.SIGTERM, None)
        killer = threading.Thread(target=fire, daemon=True)
        killer.start()
        out = io.StringIO()
        # The handler run() installs is called directly: no real signal can outlive the test.
        with contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sparrowd.signal, "signal",
                                                  side_effect=lambda s, h: handlers.__setitem__(s, h)))
            for name, fn in patches.items():
                stack.enter_context(mock.patch.object(sparrowd.ControlServer, name, fn))
            rc = sparrowd.run([], state, rescan=lambda: [], rescan_s=0.05)
        killer.join(5)
        return rc, out.getvalue()

    def test_an_untrusted_short_dir_is_logged_once_and_run_keeps_going(self):
        state = self.long_state()
        cs.short_dir().write_text("not a dir")
        rc, out = self.run_until_sigterm(state)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("control socket unavailable"), 1, out)
        self.assertIn("shutdown requested", out)

    def test_a_bind_failure_closes_the_half_made_server_and_run_keeps_going(self):
        state = self.long_state()
        closed = []

        def boom(self):
            raise OSError("bind refused")
        rc, out = self.run_until_sigterm(state, start=boom, close=lambda self: closed.append(1))
        self.assertEqual((rc, closed), (0, [1]), out)
        self.assertEqual(out.count("control socket unavailable (bind refused)"), 1, out)


class ForeignDirectory(Base):
    def test_a_foreign_owned_dir_is_refused_by_server_and_client(self):
        state = self.long_state()
        d = cs.short_dir()
        d.mkdir(mode=0o700)
        other = os.getuid() + 1
        real_lstat, real = os.lstat, os.lstat(d)

        def lstat(p, *a, **k):
            return os.stat_result(real[:4] + (other,) + real[5:]) if Path(p) == d else real_lstat(p, *a, **k)
        with mock.patch.object(cs.os, "lstat", side_effect=lstat):
            for create in (True, False):
                with self.assertRaisesRegex(cs.ControlSocketPathError, "owned by uid"):
                    cs.control_socket_path(state, create=create)

    def test_a_symlink_or_open_dir_is_refused(self):
        state = self.long_state()
        target = self.td / "elsewhere"
        target.mkdir(mode=0o700)
        cs.short_dir().symlink_to(target)
        with self.assertRaisesRegex(cs.ControlSocketPathError, "not a directory"):
            cs.control_socket_path(state, create=True)
        cs.short_dir().unlink()
        cs.short_dir().mkdir(mode=0o700)
        os.chmod(cs.short_dir(), 0o777)
        with self.assertRaisesRegex(cs.ControlSocketPathError, "open to other users"):
            cs.control_socket_path(state)
        cs.control_socket_path(state, create=True)
        self.assertEqual(os.stat(cs.short_dir()).st_mode & 0o777, 0o700, "the owner's server tightens it")

    def test_an_untrusted_dir_logs_once_and_keeps_supervising_instead_of_exiting(self):
        state = self.long_state()
        cs.short_dir().write_text("not a dir")
        proc = self.start_daemon(state)
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "sparrowd stays up: no exit for a relauncher to loop on")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out.count("control socket unavailable"), 1, out)
        self.assertIn("worker w: ", out)
        self.assertNotIn("Traceback", out)


class OnlyTheSharedFunctionBuildsSocketPaths(unittest.TestCase):
    SCOPES = ("src", "packages", "scripts", "skills", "tests")
    SUFFIXES = {".py", ".sh", ".ts", ".js", ".mjs"}

    def test_the_socket_name_is_spelled_only_in_the_shared_module(self):
        owner = PKG / "ag2_sparrow" / "control_socket.py"
        hits = []
        for scope in self.SCOPES:
            for f in (REPO / scope).rglob("*"):
                if f.suffix not in self.SUFFIXES or "node_modules" in f.parts or f == Path(__file__).resolve():
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "sparrowd.sock" in text and f.resolve() != owner:
                    hits.append(str(f.relative_to(REPO)))
        self.assertEqual(hits, [], "build the control socket path with control_socket_path()")

    def test_the_daemon_constructs_its_control_server_only_from_the_shared_function(self):
        src = (PKG / "ag2_sparrow" / "sparrowd.py").read_text(encoding="utf-8")
        calls = re.findall(r"ControlServer\(([^,]+),", src)
        self.assertTrue(calls)
        self.assertTrue(all(c.strip().startswith("control_socket_path(") for c in calls), calls)
        self.assertNotRegex(src, r"\.sock[\"']")


if __name__ == "__main__":
    unittest.main()
