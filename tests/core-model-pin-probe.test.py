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
        self.assertIn("setenv -u", r["detail"])   # names the remedy

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
    `skipIf(tmux missing)`. On a CI runner without tmux they all skipped, the new
    lines got zero coverage, and `diff coverage >= 95%` failed. A test that cannot
    run where the merge is gated does not cover that code.
    """

    def setUp(self):
        self.hc = _load()

    def test_set_warns_and_names_value_and_remedy(self):
        r = self.hc._interpret_core_model_pin(0, "SUTANDO_CORE_MODEL=opus\n", "/tmp/s.sock")
        self.assertEqual(r["status"], "warn")
        self.assertIn("opus", r["detail"])
        self.assertIn("setenv -u SUTANDO_CORE_MODEL", r["detail"])
        self.assertIn("/tmp/s.sock", r["detail"])

    def test_unknown_variable_is_ok(self):
        r = self.hc._interpret_core_model_pin(1, "unknown variable: SUTANDO_CORE_MODEL", "/tmp/s.sock")
        self.assertEqual(r["status"], "ok")

    def test_empty_output_is_ok(self):
        self.assertEqual(self.hc._interpret_core_model_pin(0, "", "/tmp/s.sock")["status"], "ok")

    def test_unrelated_line_is_ok(self):
        """rc 0 with some other variable must not be read as a pin."""
        r = self.hc._interpret_core_model_pin(0, "SOMETHING_ELSE=1", "/tmp/s.sock")
        self.assertEqual(r["status"], "ok")

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

    def test_name_is_stable(self):
        for rc, out in ((0, "SUTANDO_CORE_MODEL=opus"), (1, "unknown variable: X")):
            self.assertEqual(self.hc._interpret_core_model_pin(rc, out, "/s")["name"], "core-model-pin")


if __name__ == "__main__":
    unittest.main()
