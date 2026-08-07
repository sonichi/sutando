"""The wedge-recovery model downgrade must be visible in health output.

`recover_core_if_wedged` sets SUTANDO_CORE_MODEL=opus (200K) on a re-wedge. The
value was written and read nowhere, lives in the core's tmux SESSION env, and
nothing expires it — so a peer core ran 17 days downgraded, autocompacting every
9.1 minutes, and health reported clean throughout.

Exercised against a real tmux socket in both directions, because a probe that
cannot answer both ways proves nothing.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["hc"] = m
    spec.loader.exec_module(m)
    return m


@unittest.skipIf(shutil.which("tmux") is None, "tmux not installed")
class CoreModelPinProbe(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "probe.sock")
        self._prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        os.environ["SUTANDO_TMUX_SOCKET"] = self.sock

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True)
        if self._prev is None:
            os.environ.pop("SUTANDO_TMUX_SOCKET", None)
        else:
            os.environ["SUTANDO_TMUX_SOCKET"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, *env_args):
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "core",
                        *env_args, "sleep 120"], capture_output=True)

    def test_pinned_core_warns(self):
        self._session("-e", "SUTANDO_CORE_MODEL=opus")
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("opus", r["detail"])
        self.assertIn("setenv -t '=core' -u", r["detail"])  # remedy names the SESSION
        self.assertIn("core=", r["detail"])           # and which one is pinned

    def test_sibling_session_does_not_hide_a_pinned_core(self):
        """THE REGRESSION. Two sessions, only the non-first one pinned.

        Untargeted `show-environment` resolves to whichever session tmux picks.
        With `notifier` created first, it picked the unpinned one, returned
        "unknown variable" and the probe reported ok on an actively pinned core --
        the exact silent miss it exists to prevent. Found independently by
        qingyun-wu, bassilkhilo-ag2 and sonichi; this fails against the
        untargeted implementation.
        """
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "notifier",
                        "sleep 120"], capture_output=True)
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "sutando-core",
                        "-e", "SUTANDO_CORE_MODEL=opus", "sleep 120"], capture_output=True)
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("sutando-core", r["detail"], "must name WHICH session is pinned")
        self.assertIn("opus", r["detail"])

    def test_creation_order_cannot_hide_it_either(self):
        """Same two sessions, pinned one created FIRST — order must not matter."""
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "sutando-core",
                        "-e", "SUTANDO_CORE_MODEL=opus", "sleep 120"], capture_output=True)
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "notifier",
                        "sleep 120"], capture_output=True)
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("sutando-core", r["detail"])

    def test_two_unpinned_sessions_stay_ok(self):
        """The control: the multi-session path must not invent a pin."""
        for n in ("notifier", "sutando-core"):
            subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", n,
                            "sleep 120"], capture_output=True)
        self.assertEqual(self.hc.check_core_model_pin()["status"], "ok")

    def test_unpinned_core_ok(self):
        self._session()
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "ok", r)

    def test_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_core_model_pin())", src)


class InterpretPinNoTmuxNeeded(unittest.TestCase):
    """The logic half, exercised WITHOUT tmux — these run on any runner.

    The first version of this file gated every behavioural test on
    `skipIf(tmux missing)`. On a CI runner without tmux they all skipped, so the
    new lines were never executed. A test that cannot run where the merge is
    gated does not cover that code.

    `_interpret_core_model_pin` now takes the list of pinned (session, value)
    pairs rather than one tmux result — the single-result form was the P1.
    """

    def setUp(self):
        self.hc = _load()

    def test_no_pins_is_ok(self):
        self.assertEqual(self.hc._interpret_core_model_pin([], "/tmp/s.sock")["status"], "ok")

    def test_one_pin_warns_and_names_session_value_and_remedy(self):
        r = self.hc._interpret_core_model_pin([("sutando-core", "opus")], "/tmp/s.sock")
        self.assertEqual(r["status"], "warn")
        self.assertIn("opus", r["detail"])
        self.assertIn("sutando-core", r["detail"])
        self.assertIn("setenv -t '=sutando-core' -u SUTANDO_CORE_MODEL", r["detail"])
        self.assertIn("/tmp/s.sock", r["detail"])

    def test_remedy_is_per_session_when_several_are_pinned(self):
        """A single untargeted setenv would clear only one — emit one fix each."""
        r = self.hc._interpret_core_model_pin(
            [("sutando-core", "opus"), ("second", "sonnet")], "/tmp/s.sock")
        self.assertEqual(r["status"], "warn")
        for sess in ("sutando-core", "second"):
            self.assertIn(f"setenv -t '={sess}' -u SUTANDO_CORE_MODEL", r["detail"])
        self.assertIn("sonnet", r["detail"])

    def test_name_is_stable(self):
        for pins in ([], [("core", "opus")]):
            self.assertEqual(
                self.hc._interpret_core_model_pin(pins, "/s")["name"], "core-model-pin")

    def test_absent_socket_is_ok_not_a_failure(self):
        """Covers the IO half's early return — needs no tmux, so it runs in CI."""
        import tempfile as _tf
        prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        with _tf.TemporaryDirectory() as td:
            os.environ["SUTANDO_TMUX_SOCKET"] = os.path.join(td, "absent.sock")
            try:
                r = self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("skipped", r["detail"])

    def test_tmux_query_failure_is_ok_not_a_health_failure(self):
        """A probe that cannot query must not manufacture a red health check.

        Needs no tmux: `.exists()` accepts any file, and subprocess.run is patched
        to raise. This is the arm the diff-coverage gate reported uncovered.
        """
        import tempfile as _tf
        from unittest import mock
        prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        with _tf.NamedTemporaryFile() as fake_socket:
            os.environ["SUTANDO_TMUX_SOCKET"] = fake_socket.name
            try:
                with mock.patch.object(self.hc.subprocess, "run",
                                       side_effect=OSError("tmux exploded")):
                    r = self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("tmux exploded", r["detail"])
        self.assertIn("skipped", r["detail"])


if __name__ == "__main__":
    unittest.main()
