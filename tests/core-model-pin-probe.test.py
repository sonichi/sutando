"""The wedge-recovery model downgrade must be visible in health output.
Exercised against a real tmux socket in both directions."""
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
        """Two sessions, only the non-first one pinned: an untargeted
        `show-environment` picks a session and can miss a pinned core."""
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

    def test_global_env_pin_is_not_invisible(self):
        """A pin set with `setenv -g` is absent from every per-session query, so
        enumerating sessions alone reports ok on a globally pinned socket."""
        self._session()
        subprocess.run(["tmux", "-S", self.sock, "setenv", "-g",
                        "SUTANDO_CORE_MODEL", "opus"], capture_output=True)
        probe = subprocess.run(["tmux", "-S", self.sock, "show-environment",
                                "-t", "=core", "SUTANDO_CORE_MODEL"],
                               capture_output=True, text=True)
        self.assertNotIn("SUTANDO_CORE_MODEL=opus", probe.stdout,
                         "premise: the session query must NOT see the global pin")
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("opus", r["detail"])
        self.assertIn("setenv -g -u SUTANDO_CORE_MODEL", r["detail"],
                      "the remedy must use -g; `-t '=global'` addresses no session")

    def test_unpinned_core_ok(self):
        self._session()
        r = self.hc.check_core_model_pin()
        self.assertEqual(r["status"], "ok", r)

    def test_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_core_model_pin())", src)


class InterpretPinNoTmuxNeeded(unittest.TestCase):
    """`_interpret_core_model_pin` takes the collected (session, value) pairs,
    not one tmux result, so the multi-session logic is testable without tmux."""

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

    def test_global_scope_uses_g_not_a_session_target(self):
        r = self.hc._interpret_core_model_pin([("global", "opus")], "/tmp/s.sock")
        self.assertEqual(r["status"], "warn")
        self.assertIn("tmux -S /tmp/s.sock setenv -g -u SUTANDO_CORE_MODEL", r["detail"])
        self.assertNotIn("-t '=global'", r["detail"], r)

    def test_remedy_describes_the_launcher_self_heal_not_a_re_supply(self):
        """The Claude launcher clears the pin; it no longer re-supplies it."""
        r = self.hc._interpret_core_model_pin([("sutando-core", "opus")], "/s")
        self.assertIn("self-heals", r["detail"])
        self.assertIn("Codex", r["detail"], "Codex still honors the var — say so")
        for stale in ("not durable", "re-supplies", "launching process"):
            self.assertNotIn(stale, r["detail"],
                             f"stale claim {stale!r}: the launcher no longer re-supplies")

    def test_name_is_stable(self):
        for pins in ([], [("core", "opus")]):
            self.assertEqual(
                self.hc._interpret_core_model_pin(pins, "/s")["name"], "core-model-pin")

    def test_absent_socket_is_ok_not_a_failure(self):
        """Covers the IO half's early return; needs no tmux."""
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
        Needs no tmux: `.exists()` accepts any file and subprocess.run is patched."""
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

    def test_failed_scope_query_is_not_reported_as_unset(self):
        """A nonzero `show-environment` that is NOT tmux's unset marker is a failed
        query; reading it as "not pinned" clears the probe on an uninspected scope."""
        import tempfile as _tf
        from unittest import mock
        unset = subprocess.CompletedProcess(
            args=["tmux"], returncode=1, stdout="",
            stderr="unknown variable: SUTANDO_CORE_MODEL")
        sessions = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="core\n", stderr="")
        broken = subprocess.CompletedProcess(
            args=["tmux"], returncode=1, stdout="", stderr="server lost")
        prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        with _tf.NamedTemporaryFile() as fake_socket:
            os.environ["SUTANDO_TMUX_SOCKET"] = fake_socket.name
            try:
                # global unset -> sessions ok -> the session's own query FAILS
                with mock.patch.object(self.hc.subprocess, "run",
                                       side_effect=[unset, sessions, broken]):
                    r = self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
        self.assertIn("skipped", r["detail"], "an uninspected scope must not read as clear")
        self.assertNotIn("no model pin", r["detail"], r)

    def test_unset_marker_on_either_stream_still_means_unset(self):
        """The control: tmux's own unset shape must stay a clean ok, not an error."""
        for stream in ("stdout", "stderr"):
            kw = {stream: "unknown variable: SUTANDO_CORE_MODEL"}
            kw.setdefault("stdout", ""), kw.setdefault("stderr", "")
            res = subprocess.CompletedProcess(args=["tmux"], returncode=1, **kw)
            from unittest import mock
            with mock.patch.object(self.hc.subprocess, "run", return_value=res):
                self.assertEqual(self.hc._query_pin("/s", ["-g"]), "", stream)

    def test_failed_enumeration_is_not_reported_as_no_pin(self):
        """A nonzero `list-sessions` must not become an empty session list —
        that would clear the probe without inspecting a single session."""
        import tempfile as _tf
        from unittest import mock
        prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        with _tf.NamedTemporaryFile() as fake_socket:
            os.environ["SUTANDO_TMUX_SOCKET"] = fake_socket.name
            try:
                with mock.patch.object(
                    self.hc.subprocess, "run",
                    return_value=subprocess.CompletedProcess(
                        args=["tmux"], returncode=1, stdout="", stderr="no server running"),
                ):
                    r = self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
        self.assertIn("skipped", r["detail"], "must say it could not query, not that it found nothing")
        self.assertNotIn("no model pin", r["detail"], r)


if __name__ == "__main__":
    unittest.main()
