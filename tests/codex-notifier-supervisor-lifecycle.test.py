#!/usr/bin/env python3
"""Lifecycle contract for src/agent/codex/cli/task-notifier-supervisor.sh.

The supervisor is the only thing keeping the Codex task notifier alive, so its
failure handling is load-bearing: a fixed 1s respawn turns a permanent
configuration fault into a crash loop, and a second supervisor silently doubles
every wakeup the notifier injects.

Timing is asserted through the supervisor's own "restarting in Ns" log lines
rather than measured wall-clock gaps -- a sleep-timing assertion is flaky under
CI load, and a flaky guard is worse than none.
"""
import os
import time
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"


def _write(path: Path, body: str, executable: bool = False) -> Path:
    path.write_text(textwrap.dedent(body).lstrip())
    if executable:
        path.chmod(0o755)
    return path


class SupervisorLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runs = self.d / "runs"
        self.lock = self.d / "lease.lock"

    def _fake_tmux(self, alive_calls: int) -> Path:
        """`tmux has-session` succeeds alive_calls times, then reports gone."""
        return _write(self.d / "tmux", f"""
            #!/bin/bash
            C="{self.d}/tmux.calls"
            n=$(cat "$C" 2>/dev/null || echo 0)
            echo $((n+1)) > "$C"
            [ "$n" -lt "{alive_calls}" ]
        """, executable=True)

    def _notifier(self, exit_code: int) -> Path:
        """Records which pid owns the lease while the notifier runs. Recording
        mere directory existence cannot tell a lease this supervisor took from a
        stale one a test planted, so both lease assertions would pass unfixed."""
        return _write(self.d / "notifier.sh", f"""
            #!/bin/bash
            echo "run:$(cat '{self.lock}/pid' 2>/dev/null || echo none)" >> "{self.runs}"
            exit {exit_code}
        """, executable=True)

    def lease_owners_seen(self) -> list:
        if not self.runs.exists():
            return []
        return [ln.split(":", 1)[1] for ln in self.runs.read_text().split() if ln.startswith("run:")]

    def _run(self, alive_calls=6, exit_code=1, env_extra=None):
        self._fake_tmux(alive_calls)
        notifier = self._notifier(exit_code)
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.d}:{env['PATH']}",
            "SUTANDO_NOTIFIER_SCRIPT": str(notifier),
            "SUTANDO_NOTIFIER_LOCK_DIR": str(self.lock),
            "SUTANDO_NOTIFIER_RESTART_DELAY": "1",
            "SUTANDO_NOTIFIER_RESTART_DELAY_MAX": "4",
            "SUTANDO_NOTIFIER_STABLE_AFTER": "600",
        })
        env.update(env_extra or {})
        return subprocess.run(["bash", str(SUPERVISOR)], env=env,
                              capture_output=True, text=True, timeout=90)

    def run_count(self) -> int:
        return len(self.runs.read_text().split()) if self.runs.exists() else 0

    def test_backoff_grows_and_is_capped(self):
        """A crash-looping notifier must not be respawned at a fixed 1s."""
        r = self._run(alive_calls=8, exit_code=1)
        delays = [int(w.rstrip("s.")) for line in r.stderr.splitlines()
                  if "restarting in" in line
                  for w in [line.rsplit(" ", 1)[-1]]]
        self.assertGreaterEqual(len(delays), 3, f"expected several restarts, got {r.stderr}")
        self.assertEqual(delays[:3], [1, 2, 4], "backoff must double from the base delay")
        self.assertTrue(all(d <= 4 for d in delays), f"backoff exceeded its cap: {delays}")

    def test_fractional_restart_delay_still_restarts(self):
        """The delay is documented as fractional and the launcher suite drives it
        at 0.01. Bash arithmetic is integer-only, so doubling it with $(( )) kills
        the supervisor after one spawn instead of backing off."""
        r = self._run(alive_calls=6, exit_code=1,
                      env_extra={"SUTANDO_NOTIFIER_RESTART_DELAY": "0.01",
                                 "SUTANDO_NOTIFIER_RESTART_DELAY_MAX": "0.05"})
        self.assertGreaterEqual(self.run_count(), 2,
                                f"fractional delay must not stop the loop: {r.stderr}")
        self.assertNotIn("syntax error", r.stderr)

    def test_configuration_fault_is_terminal(self):
        """Exit 2 is a usage fault; respawning re-runs the same broken call."""
        r = self._run(alive_calls=8, exit_code=2)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not restarting", r.stderr)
        self.assertEqual(self.run_count(), 1, "a configuration fault must not respawn")

    def test_second_supervisor_defers_to_a_live_lease_holder(self):
        """Two supervisors would double every notification injected into Codex."""
        self.lock.mkdir()
        (self.lock / "pid").write_text(f"{os.getpid()}\n")
        r = self._run(alive_calls=8, exit_code=1)
        self.assertEqual(r.returncode, 0)
        self.assertIn("already supervises", r.stderr)
        self.assertEqual(self.run_count(), 0, "the loser must not spawn a notifier")

    def test_stale_lease_is_reclaimed(self):
        """A lease whose owner died is stale, not contended -- else one crash
        wedges the notifier permanently."""
        self.lock.mkdir()
        dead = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True)
        dead_pid = dead.stdout.strip()
        (self.lock / "pid").write_text(dead_pid + "\n")
        r = self._run(alive_calls=2, exit_code=1)
        self.assertNotIn("already supervises", r.stderr)
        owners = self.lease_owners_seen()
        self.assertGreaterEqual(len(owners), 1, "stale lease must be reclaimed")
        self.assertNotIn(dead_pid, owners,
                         "the dead owner's lease was left in place, not retaken")
        self.assertNotIn("none", owners, "reclaiming must take the lease")

    def test_lease_is_held_during_the_run_and_released_after(self):
        self._run(alive_calls=1, exit_code=1)
        owners = self.lease_owners_seen()
        self.assertTrue(owners and "none" not in owners, f"lease never taken: {owners}")
        self.assertFalse(self.lock.exists(), "lease outlived the supervisor")


class LeaseOwnershipTest(unittest.TestCase):
    """Contention controls. Every one drives the production supervisor: a
    hand-rolled recipe would certify a script the repo does not ship."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runs = self.d / "runs"
        self.lock = self.d / "lease.lock"
        self.stop = self.d / "stop"
        # Alive until the test says otherwise, so two supervisors can overlap.
        _write(self.d / "tmux", f"""
            #!/bin/bash
            [ -f "{self.stop}" ] && exit 1
            exit 0
        """, executable=True)

    def _notifier(self, tag):
        n = _write(self.d / f"notifier-{tag}.sh", f"""
            #!/bin/bash
            echo "{tag}" >> "{self.runs}"
            sleep 10
        """, executable=True)
        return n

    def _env(self, tag, **extra):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.d}:{env['PATH']}",
            "TMPDIR": str(self.d),
            "SUTANDO_NOTIFIER_SCRIPT": str(self._notifier(tag)),
            "SUTANDO_NOTIFIER_RESTART_DELAY": "1",
            "SUTANDO_NOTIFIER_RESTART_DELAY_MAX": "2",
            "SUTANDO_NOTIFIER_STABLE_AFTER": "600",
        })
        env.pop("SUTANDO_NOTIFIER_LOCK_DIR", None)
        env.update(extra)
        return env

    def _run(self, tag, **extra):
        return subprocess.run(["bash", str(SUPERVISOR)], env=self._env(tag, **extra),
                              capture_output=True, text=True, timeout=60)

    def _bg(self, tag, **extra):
        proc = subprocess.Popen(["bash", str(SUPERVISOR)], env=self._env(tag, **extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        return proc

    def _reap(self, proc):
        # SIGTERM, not the tmux stop file: the supervisor is blocked in `wait`
        # on its child, so polling teardown costs a whole notifier lifetime.
        self.stop.touch()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def _ran(self):
        return self.runs.read_text().split() if self.runs.exists() else []

    def _await_run(self, tag, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if tag in self._ran():
                return
            time.sleep(0.2)
        self.fail(f"supervisor {tag} never started its notifier; ran={self._ran()}")

    # --- incomplete publication (kewei's P1 repro: mkdir before metadata) ---

    def test_incomplete_publication_defers_rather_than_deleting(self):
        """A directory with no metadata is a winner caught mid-publication.
        Deleting it hands a second supervisor the lease the first one holds."""
        self.lock.mkdir()
        r = self._run("second", SUTANDO_NOTIFIER_LOCK_DIR=str(self.lock))
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("second", self._ran(), "ran while a winner was publishing")
        self.assertTrue(self.lock.exists(), "deleted a publishing winner's lease")

    def test_incomplete_publication_is_reclaimed_once_stale(self):
        """The defer above is bounded: a crash inside publication must not
        wedge the notifier off forever."""
        self.lock.mkdir()
        old = time.time() - 600
        os.utime(self.lock, (old, old))
        self._bg("late", SUTANDO_NOTIFIER_LOCK_DIR=str(self.lock),
                 SUTANDO_NOTIFIER_PUBLISH_GRACE="10")
        self._await_run("late")

    # --- former owner must not delete its successor ---

    def test_former_owner_leaves_its_successors_lease_alone(self):
        holder = self._bg("holder", SUTANDO_NOTIFIER_LOCK_DIR=str(self.lock))
        self._await_run("holder")
        successor = (self.lock / "token").read_text()

        # Reclaim it out from under the holder, as a stale-lease sweep would.
        (self.lock / "token").write_text("999999:elsewhere\n")
        (self.lock / "pid").write_text("999999\n")

        holder.terminate()   # runs the trap -> release_lease, the path under test
        holder.wait(timeout=15)
        self.assertTrue(self.lock.exists(),
                        "the former owner deleted a lease it no longer held")
        self.assertNotEqual((self.lock / "token").read_text(), successor)

    def test_an_unreadable_mtime_defers_instead_of_reclaiming(self):
        """The grace check reads mtime through `stat`, whose flags differ by
        platform. A variant that prints non-numeric text and exits 0 must not
        turn the guard into the lease-steal it exists to prevent."""
        _write(self.d / "stat", """
            #!/bin/bash
            echo '?'
            exit 0
        """, executable=True)
        self.lock.mkdir()
        r = self._run("blind", SUTANDO_NOTIFIER_LOCK_DIR=str(self.lock))
        self.assertEqual(r.returncode, 0)
        self.assertIn("publishing", r.stderr)
        self.assertNotIn("blind", self._ran(), "an unreadable mtime let it steal the lease")
        self.assertTrue(self.lock.exists())

    # --- P2: the lease is keyed per (socket, session), as start-cli launches ---

    def test_a_second_socket_does_not_suppress_the_first(self):
        self._bg("A", SUTANDO_TMUX_SOCKET="/tmp/sock-A", SUTANDO_TMUX_SESSION="s")
        self._await_run("A")

        same = self._run("same", SUTANDO_TMUX_SOCKET="/tmp/sock-A",
                         SUTANDO_TMUX_SESSION="s")
        self.assertEqual(same.returncode, 0)
        self.assertNotIn("same", self._ran(),
                         "two notifiers on one (socket, session) — the lease failed")

        other = self._bg("B", SUTANDO_TMUX_SOCKET="/tmp/sock-B",
                         SUTANDO_TMUX_SESSION="s")
        self._await_run("B")
        self.assertIsNone(other.poll(), "a second socket was suppressed by the first")

    # --- cleanup must not be recursive on a configurable path ---

    def test_cleanup_never_removes_unrelated_contents(self):
        self.lock.mkdir()
        keep = self.lock / "not-ours"
        keep.write_text("someone else's data\n")
        old = time.time() - 600
        os.utime(self.lock, (old, old))
        r = self._run("recurse", SUTANDO_NOTIFIER_LOCK_DIR=str(self.lock),
                      SUTANDO_NOTIFIER_PUBLISH_GRACE="10")
        self.assertTrue(keep.exists(),
                        "supervisor deleted a pre-existing file under the lock path")
        # Fail closed and say which path: refusing to run beats emptying a
        # directory that is not ours because it sits at a configurable path.
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(str(self.lock), r.stderr)
        self.assertNotIn("recurse", self._ran())


if __name__ == "__main__":
    unittest.main()
