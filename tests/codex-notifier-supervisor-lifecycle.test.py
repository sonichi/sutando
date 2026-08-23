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


if __name__ == "__main__":
    unittest.main()
