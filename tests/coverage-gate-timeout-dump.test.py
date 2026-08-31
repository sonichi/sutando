#!/usr/bin/env python3
"""A capped test file must say WHERE it sat, not only that it timed out.

#3630: `outbox-race.test.py` exceeded the per-file cap in CI while every local
measurement (exact gate invocation, forced fork, saturated CPU) finished in
seconds — a slow-vs-hang question the gate's output could not answer, because
`timeout`'s default TERM kills the interpreter silently. The gate now sends
ABRT with PYTHONFAULTHANDLER=1 in the environment: faulthandler dumps every
thread's stack (file:line) as the process dies, the gate's existing
echo-output-on-failure path prints it, and the next natural occurrence
self-diagnoses. GNU timeout still exits 124 on expiry regardless of signal,
so the TIMED OUT detection branch (rc 124/137) is unchanged.

Run: python3 tests/coverage-gate-timeout-dump.test.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(REPO, "scripts", "coverage-gate.sh")


class DumpMechanism(unittest.TestCase):
    """The contract the gate now relies on: ABRT + PYTHONFAULTHANDLER=1
    makes a wedged interpreter print every thread's stack on the way out."""

    def _run_hang_probe(self, env_extra):
        probe = (
            "import threading, time\n"
            "t = threading.Thread(target=time.sleep, args=(600,), name='stuck-worker')\n"
            "t.start()\n"
            "time.sleep(600)\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(probe)
            path = f.name
        try:
            # Build the child env deterministically: the gate itself exports
            # PYTHONFAULTHANDLER=1, so inheriting os.environ arms the control.
            env = dict(os.environ)
            env.pop("PYTHONFAULTHANDLER", None)
            env.update(env_extra)
            p = subprocess.Popen(
                [sys.executable, path], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                # The supervisor plays timeout(1)'s role: ABRT at the cap.
                import time
                time.sleep(1.5)
                p.send_signal(signal.SIGABRT)
                out, _ = p.communicate(timeout=15)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            return out
        finally:
            os.unlink(path)

    def test_abrt_with_faulthandler_names_every_threads_position(self):
        out = self._run_hang_probe({"PYTHONFAULTHANDLER": "1"})
        self.assertIn("Fatal Python error: Aborted", out)
        # Both threads are located by file:line. Assert the version-stable
        # shape: 3.12 prints bare "Thread 0x..." headers (names arrived later).
        self.assertIn("Current thread 0x", out)
        self.assertRegex(out, r"(?m)^Thread 0x")
        self.assertRegex(out, r"File .*, line \d+ in <module>")

    def test_without_faulthandler_the_death_is_silent(self):
        # Negative control: the dump comes from the env var, not from ABRT
        # alone — remove the variable and the diagnostic disappears.
        out = self._run_hang_probe({})
        self.assertNotIn("Fatal Python error", out)
        self.assertNotIn("Current thread 0x", out)


class GateWiring(unittest.TestCase):
    """The gate actually arms the mechanism (both halves, same invocation)."""

    def setUp(self):
        with open(GATE) as f:
            self.src = f.read()

    def test_timeout_sends_abrt(self):
        self.assertIn("timeout -k 5 -s ABRT $COVERAGE_GATE_FILE_TIMEOUT", self.src)

    def test_lane_env_arms_faulthandler(self):
        # Same env(1) invocation that carries the coverage flag — a dump-armed
        # timeout in one lane runner and not another would be a silent gap.
        self.assertIn(
            "env SUTANDO_TEST_SUBPROCESS_COVERAGE=1 PYTHONFAULTHANDLER=1 $COVGATE_TIMEOUT",
            self.src,
        )

    def test_timed_out_branch_still_keys_on_124(self):
        # ABRT must not have moved the detection: GNU timeout exits 124 on
        # expiry for any -s signal (137 is the -k KILL escalation).
        self.assertIn('[ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]', self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
