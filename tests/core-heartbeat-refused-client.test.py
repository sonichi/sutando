"""run_forever() must not confirm death on a probe that observed nothing.

A tmux client of another version than the server exits 1 with "server exited
unexpectedly" before any session lookup. Before this suite, core_pid() mapped
that to "gone" and three beats later run_forever() unlinked the live core's
.alive — the one signal that was keeping the supervisor gate from acting.
Drives the PRODUCTION loop with _tmux stubbed at the I/O seam.
"""
import os
import subprocess as sp
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import core_heartbeat as ch  # noqa: E402


def _cp(rc, stderr=""):
    return sp.CompletedProcess(["tmux"], rc, "", stderr)


class RefusedClient(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.alive = self.tmp / "testhost.alive"
        self._orig = (ch._tmux, ch.core_pid, ch._alive_path, ch.write_beat, ch._SHUTDOWN_REQUESTED)
        ch._alive_path = lambda: self.alive
        ch._SHUTDOWN_REQUESTED = False

    def tearDown(self):
        ch._tmux, ch.core_pid, ch._alive_path, ch.write_beat, ch._SHUTDOWN_REQUESTED = self._orig

    def _run_with(self, has_session_results, beats):
        """core_pid is REAL; has-session answers come from the seam in order."""
        answers = list(has_session_results)

        def fake_tmux(sock, *a):
            if a and a[0] == "has-session":
                return answers.pop(0) if answers else answers_last
            return _cp(1, "")
        answers_last = has_session_results[-1]
        ch._tmux = fake_tmux
        # pgrep/ps never find a core in this harness, so a PRESENT session
        # still yields no pid; a present beat is injected via core_pid instead.
        count = {"n": 0}
        real_pid = self._orig[1]

        def counted_core_pid(socket_path=None, session=None):
            count["n"] += 1
            if count["n"] == 1:
                ch._LAST_SESSION_PROBE = True   # the one observed-present beat
                return 4242
            return real_pid(socket_path, session)
        ch.core_pid = counted_core_pid

        def beat(status=None):
            self.alive.write_text("{}")
            if count["n"] >= beats:
                ch._SHUTDOWN_REQUESTED = True
        ch.write_beat = beat
        rc = ch.run_forever(interval=0.01, status="test")
        return rc, count["n"]

    def test_refused_client_holds_the_streak_and_keeps_alive(self):
        refused = _cp(1, "server exited unexpectedly\n")
        rc, n = self._run_with([refused] * 6, beats=6)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(n, 6, "loop should have kept beating, not died")
        self.assertTrue(self.alive.exists(), ".alive was unlinked on unobserved probes")

    def test_observed_absence_still_confirms_death(self):
        gone = _cp(1, "can't find session: sutando-core\n")
        rc, n = self._run_with([gone] * 6, beats=50)
        self.assertEqual(rc, 0)
        self.assertEqual(n, 1 + ch.ABSENT_BEATS_BEFORE_DEATH)
        self.assertFalse(self.alive.exists())

    def test_unobserved_does_not_reset_an_absence_streak(self):
        # absent, absent, unobserved, absent → the third absence lands on beat 5.
        seq = [_cp(1, "can't find session: x\n")] * 2 + [_cp(1, "server exited unexpectedly\n")] + [_cp(1, "can't find session: x\n")] * 3
        rc, n = self._run_with(seq, beats=50)
        self.assertEqual(rc, 0)
        self.assertEqual(n, 1 + ch.ABSENT_BEATS_BEFORE_DEATH + 1)
        self.assertFalse(self.alive.exists())

    def test_session_present_records_tristate(self):
        ch._tmux = lambda sock, *a: _cp(1, "server exited unexpectedly\n")
        self.assertIsNone(ch._session_present("s.sock", "core"))
        self.assertIsNone(ch._LAST_SESSION_PROBE)
        ch._tmux = lambda sock, *a: _cp(1, "can't find session: core\n")
        self.assertIs(ch._session_present("s.sock", "core"), False)
        ch._tmux = lambda sock, *a: None
        self.assertIsNone(ch._session_present("s.sock", "core"))


if __name__ == "__main__":
    unittest.main()
