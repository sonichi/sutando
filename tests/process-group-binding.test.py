#!/usr/bin/env python3
"""A fixture group bound to a test dies with that test, however the test ends.

Each case runs a stand-in "test" process that starts a fixture group, binds it
with tests/_helpers/process_group.py and then exits normally, is SIGTERMed or
is SIGKILLed. The fixture must be gone afterwards; an unbound group, and a
group whose leader pid was recycled, must not be touched. Also pinned:
OWNER_LIFELINE_PY ends a fixture once its owner is gone, or once the owner pid
belongs to a process with another start time.

Run: python3 tests/process-group-binding.test.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _helpers import process_group as pg  # noqa: E402


def alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def wait_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} never written")


OWNER = textwrap.dedent("""
    import os, subprocess, sys, time
    sys.path.insert(0, {tests!r})
    from _helpers import process_group as pg
    fixture = subprocess.Popen(["sleep", "300"], start_new_session=True)
    {bind}
    with open({out!r}, "w") as f:
        f.write(str(fixture.pid))
    {end}
""")


class BoundGroupDiesWithTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pg-binding-"))
        self.started = []

    def tearDown(self):
        for pid, lstart in self.started:   # only what this case started, verified by start time
            if lstart and pg.start_time(pid) == lstart:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass

    def owner(self, end: str, bind: str = "pg.bind_to_test(fixture.pid)"):
        out = self.tmp / "fixture.pid"
        code = OWNER.format(tests=str(TESTS), bind=bind, out=str(out), end=end)
        proc = subprocess.Popen([sys.executable, "-c", code])
        pid = int(wait_file(out))
        self.started.append((pid, pg.start_time(pid)))
        self.assertTrue(alive(pid), "the fixture never ran; the case proves nothing")
        return proc, pid

    def test_gone_after_a_normal_exit(self):
        go = self.tmp / "go"
        proc, pid = self.owner(f"while not os.path.exists({str(go)!r}): time.sleep(0.02)")
        go.touch()
        proc.wait(timeout=10)
        self.assertTrue(wait_gone(pid), "a bound fixture outlived a test that returned")

    def test_gone_after_sigterm(self):
        proc, pid = self.owner("time.sleep(300)")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertTrue(wait_gone(pid), "a bound fixture outlived a SIGTERMed test")

    def test_gone_after_sigkill(self):
        proc, pid = self.owner("time.sleep(300)")
        proc.kill()
        proc.wait(timeout=10)
        self.assertTrue(wait_gone(pid), "a bound fixture outlived a SIGKILLed test")

    def test_unbound_group_is_left_alone(self):
        proc, pid = self.owner("pg.unbind(fixture.pid)")
        proc.wait(timeout=10)
        time.sleep(1.0)
        self.assertTrue(alive(pid), "an unbound group was killed")

    def test_recycled_leader_is_left_alone(self):
        proc, pid = self.owner("pass", bind='pg._send(f"+ {fixture.pid} Thu Jan  1 00:00:00 1970")')
        proc.wait(timeout=10)
        time.sleep(1.0)
        self.assertTrue(alive(pid), "a group whose leader start time differs was signalled")


class OwnerLifeline(unittest.TestCase):
    def test_fixture_exits_when_its_owner_is_sigtermed(self):
        owner = subprocess.Popen(["sleep", "300"])
        fixture = subprocess.Popen(
            [sys.executable, "-c", pg.OWNER_LIFELINE_PY + "\nimport time\ntime.sleep(300)\n"],
            env=dict(os.environ, **pg.owner_env(owner.pid)))
        try:
            time.sleep(1.0)
            self.assertIsNone(fixture.poll(), "the fixture exited while its owner was alive")
            owner.send_signal(signal.SIGTERM)
            owner.wait(timeout=10)
            self.assertEqual(fixture.wait(timeout=5), 0, "the fixture outlived its owner")
        finally:
            for p in (owner, fixture):
                if p.poll() is None:
                    p.kill()
                    p.wait()


    def test_fixture_exits_when_the_owner_pid_has_another_start_time(self):
        owner = subprocess.Popen(["sleep", "300"])
        env = dict(os.environ, FIXTURE_OWNER_PID=str(owner.pid), FIXTURE_OWNER_START="Thu Jan 1 00:00:00 1970")
        fixture = subprocess.Popen([sys.executable, "-c", pg.OWNER_LIFELINE_PY + "\nimport time\ntime.sleep(300)\n"],
                                   env=env)
        try:
            self.assertEqual(fixture.wait(timeout=10), 0, "a recycled owner pid kept the fixture alive")
            self.assertIsNone(owner.poll(), "the owner itself must still be running for this case")
        finally:
            for p in (owner, fixture):
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def test_owner_env_records_pid_and_start_time(self):
        env = pg.owner_env()
        self.assertEqual(env["FIXTURE_OWNER_PID"], str(os.getpid()))
        self.assertEqual(env["FIXTURE_OWNER_START"], pg.start_time(os.getpid()))
        self.assertTrue(env["FIXTURE_OWNER_START"])


if __name__ == "__main__":
    unittest.main()
