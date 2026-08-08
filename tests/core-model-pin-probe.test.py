"""The wedge-recovery model downgrade must be visible in health output.
Exercised against a real tmux socket in both directions."""
import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# A tmux server inherits its GLOBAL env from whoever starts it, so an ambient pin
# would make every temp server below born pinned — and only on an affected host.
os.environ.pop("SUTANDO_CORE_MODEL", None)

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

    def _assert_socket_starts_unpinned(self):
        """State the precondition instead of assuming it. Call AFTER the server is up:
        a socket with no server answers nothing, which would pass vacuously."""
        live = subprocess.run(["tmux", "-S", self.sock, "list-sessions"],
                              capture_output=True, text=True)
        self.assertEqual(live.returncode, 0,
                         "no tmux server on the socket — this check would pass vacuously")
        # show-environment exits 1 both when unset and when serverless, so server
        # presence is established separately above rather than read from this rc.
        got = subprocess.run(["tmux", "-S", self.sock, "show-environment", "-g",
                              "SUTANDO_CORE_MODEL"], capture_output=True, text=True).stdout
        self.assertNotIn("SUTANDO_CORE_MODEL=", got,
                         f"fixture did not start unpinned: {got.strip()!r}")

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
        self._assert_socket_starts_unpinned()
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
        self._assert_socket_starts_unpinned()
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

    def test_a_live_pinned_core_warns_even_when_tmux_is_CLEAN(self):
        """argv is immutable, so clearing tmux cannot move a running core off a pin."""
        r = self.hc._interpret_core_model_pin([], "/s", [("sutando-core", "opus")])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("LIVE core", r["detail"])
        self.assertIn("argv", r["detail"])
        self.assertIn("already clear", r["detail"],
                      "must explain why no env pin shows")

    def test_both_pinned_names_both(self):
        r = self.hc._interpret_core_model_pin(
            [("global", "opus")], "/s", [("sutando-core", "opus")])
        self.assertEqual(r["status"], "warn")
        self.assertIn("ALSO pinned", r["detail"])

    def test_unreadable_argv_WARNS_because_status_is_the_machine_channel(self):
        """INVERTED from asserting ok: emit_task_for_failures() gates on status, so a
        caveat carried only in `detail` reaches the human channel and nothing else."""
        r = self.hc._interpret_core_model_pin([], "/s", [("sutando-core", None)])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not read argv", r["detail"])
        self.assertIn("sutando-core", r["detail"])
        self.assertIn("cannot be confirmed unpinned", r["detail"])

    def test_empty_string_argv_is_unverified_not_clean(self):
        """`("sutando-core", "")` is one `is None` away from being read as a clean
        core; an empty ps read confirms nothing."""
        r = self.hc._interpret_core_model_pin([], "/s", [("sutando-core", "")])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not read argv", r["detail"])

    def test_no_core_session_stays_ok_that_is_the_discriminator(self):
        """Nothing to inspect is NOT the same as inspected-and-unreadable. If both
        warned, the probe would warn on every host with no core running."""
        r = self.hc._interpret_core_model_pin([], "/s", [])
        self.assertEqual(r["status"], "ok", r)
        self.assertNotIn("could not read argv", r["detail"])

    def test_a_session_with_no_claude_pane_is_not_an_unverified_core(self):
        """A session holding no claude process has no core to BE pinned — that is the
        core-liveness probes' question. Reporting it here warned on `notifier`."""
        with mock.patch.object(self.hc.subprocess, "run") as m:
            m.side_effect = [mock.Mock(stdout="4242\n", returncode=0),
                             mock.Mock(stdout="sleep 120\n", returncode=0)]
            self.assertEqual(self.hc._core_argv_pins("/s", ["notifier"]), [])

    def test_a_SUCCESSFUL_list_panes_that_enumerates_NOTHING_is_unknown(self):
        """A `list-panes` exiting 0 with no pid enumerated nothing. A live session always
        has >=1 pane, so this is unknown — a vanished session exits nonzero instead."""
        for label, out in (("empty stdout", ""), ("non-numeric", "%12\n%13\n")):
            def fake_run(cmd, _o=out, **kw):
                if "list-panes" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, _o, "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with self.subTest(shape=label):
                with mock.patch.object(self.hc.subprocess, "run", side_effect=fake_run):
                    rows = self.hc._core_argv_pins("/s", ["sutando-core"])
                self.assertEqual(rows, [("sutando-core", None)], f"{label}: {rows}")
                self.assertEqual(
                    self.hc._interpret_core_model_pin([], "/s", rows)["status"], "warn",
                    f"{label}: a session we could not enumerate must not read clean")

    def test_a_MIXED_pane_session_is_unknown_one_readable_claude_does_not_clear_it(self):
        """One READABLE unpinned pane must not suppress an UNREADABLE sibling: the pane
        that failed may be the still-pinned core after its tmux evidence was cleared."""
        def fake_run(cmd, **kw):
            if "list-panes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "111\n222\n", "")
            if cmd[0] == "ps":
                if cmd[-1] == "111":
                    return subprocess.CompletedProcess(cmd, 0, "claude --name sutando-core\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.hc.subprocess, "run", side_effect=fake_run):
            rows = self.hc._core_argv_pins("/s", ["sutando-core"])
        self.assertEqual(rows, [("sutando-core", None)], rows)
        self.assertEqual(
            self.hc._interpret_core_model_pin([], "/s", rows)["status"], "warn",
            "a session with ANY unread pane cannot be reported clean")

    def test_all_panes_readable_and_unpinned_stays_clean(self):
        """Discriminator: every read succeeds and nothing is pinned, so this must stay ok
        or widening the guard to `if read_failed` would warn on every multi-pane session."""
        def fake_run(cmd, **kw):
            if "list-panes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "111\n222\n", "")
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, "claude --name sutando-core\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.hc.subprocess, "run", side_effect=fake_run):
            rows = self.hc._core_argv_pins("/s", ["sutando-core"])
        self.assertEqual(rows, [], rows)
        self.assertEqual(self.hc._interpret_core_model_pin([], "/s", rows)["status"], "ok")

    def test_an_EMPTY_successful_ps_read_is_unknown_not_a_missing_pin(self):
        """Collector half of the empty-argv case: rc 0 with empty stdout read nothing, so
        the pane cannot be ruled out as claude and must not count as inspected."""
        def fake_run(cmd, **kw):
            if "list-panes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "4242\n", "")
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.hc.subprocess, "run", side_effect=fake_run):
            rows = self.hc._core_argv_pins("/s", ["sutando-core"])
        self.assertEqual(rows, [("sutando-core", None)], rows)
        self.assertEqual(
            self.hc._interpret_core_model_pin([], "/s", rows)["status"], "warn",
            "an unverified core must not reach the owner as a clean probe")

    def test_a_nonempty_non_claude_pane_stays_clean(self):
        """Discriminator: a pane that genuinely is readable and is not claude must stay ok,
        or the fix above would warn on every notifier session."""
        def fake_run(cmd, **kw):
            if "list-panes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "4242\n", "")
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(cmd, 0, "-zsh\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.hc.subprocess, "run", side_effect=fake_run):
            rows = self.hc._core_argv_pins("/s", ["notifier"])
        self.assertEqual(rows, [], rows)
        self.assertEqual(self.hc._interpret_core_model_pin([], "/s", rows)["status"], "ok")

    def test_a_FAILED_ps_read_is_unknown_not_a_missing_pin(self):
        """A transient or permission-denied `ps` made the collector return [], reporting a
        still-pinned live core as healthy. rc!=0 is unknown, not an absent pin."""
        with mock.patch.object(self.hc.subprocess, "run") as m:
            m.side_effect = [mock.Mock(stdout="4242\n", returncode=0),   # list-panes ok
                             mock.Mock(stdout="", returncode=1)]          # ps FAILED
            self.assertEqual(self.hc._core_argv_pins("/s", ["sutando-core"]),
                             [("sutando-core", None)])

    def test_a_FAILED_list_panes_is_unknown_too(self):
        """The same hole one call earlier: tmux failing left pids empty, which was
        indistinguishable from a session that genuinely has no panes."""
        with mock.patch.object(self.hc.subprocess, "run",
                               return_value=mock.Mock(stdout="", returncode=1)):
            self.assertEqual(self.hc._core_argv_pins("/s", ["sutando-core"]),
                             [("sutando-core", None)])

    def test_a_failed_read_reaches_the_probe_as_warn_end_to_end(self):
        """The wiring: unknown must survive into the status, not just the pins list."""
        r = self.hc._interpret_core_model_pin([], "/s", [("sutando-core", None)])
        self.assertEqual(r["status"], "warn", r)

    def test_clean_tmux_and_clean_argv_is_ok(self):
        r = self.hc._interpret_core_model_pin([], "/s", [])
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("LIVE core", r["detail"])

    def test_a_session_that_ENUMERATES_NOTHING_is_unknown_not_skipped(self):
        """REVERSED: a vanishing session exits nonzero (`can't find window`) and is handled
        above, so rc 0 with no pids is an enumeration that read nothing, not a skip."""
        with mock.patch.object(self.hc.subprocess, "run",
                               return_value=mock.Mock(stdout="\n", returncode=0)):
            self.assertEqual(self.hc._core_argv_pins("/s", ["sutando-core"]),
                             [("sutando-core", None)])

    def test_a_tmux_or_ps_failure_reports_unreadable_rather_than_clean(self):
        """Fail-safe direction: an exception must not read as "no pin found"."""
        with mock.patch.object(self.hc.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(self.hc._core_argv_pins("/s", ["sutando-core"]),
                             [("sutando-core", None)])

    def _check_with_socket(self, **patches):
        """Drive the real entry point against an existing socket, with helpers stubbed."""
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, "exists.sock")
            Path(sock).write_text("")
            prev = os.environ.get("SUTANDO_TMUX_SOCKET")
            os.environ["SUTANDO_TMUX_SOCKET"] = sock
            try:
                with contextlib.ExitStack() as stack:
                    for attr, kw in patches.items():
                        stack.enter_context(mock.patch.object(self.hc, attr, **kw))
                    return self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev

    def test_FIRST_pass_query_failure_warns_it_inspected_nothing(self):
        """Control 1 — the pin-query pass raising read no scope at all; ok there is the
        silent direction, since emit_task_for_failures() gates on status."""
        r = self._check_with_socket(
            _query_pin={"side_effect": OSError("permission denied")},
            _tmux_sessions={"return_value": []},
        )
        self.assertEqual(r["name"], "core-model-pin")
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not query", r["detail"])

    def test_check_survives_a_later_session_listing_failure(self):
        """Control 2 — the argv pass re-lists sessions; that raising must not escape the
        always-on run, nor read as clean when it inspected no core."""
        r = self._check_with_socket(
            _tmux_sessions={"side_effect": [[], OSError("boom")]},
            _query_pin={"return_value": None},
        )
        self.assertEqual(r["name"], "core-model-pin")
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not enumerate", r["detail"])

    def test_a_STALE_socket_file_is_ok_not_a_permanent_warn(self):
        """A socket FILE outlives its server, so every call fails with a no-server marker
        on an ordinary host; warning there would be a red that never clears."""
        err = subprocess.CalledProcessError(1, ["tmux"], "",
                                            "error connecting to /tmp/x.sock (No such file or directory)")
        r = self._check_with_socket(_query_pin={"side_effect": err})
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("no tmux server", r["detail"])

    def test_no_server_on_the_argv_pass_still_reports_a_tmux_pin(self):
        """No server on the second pass means no core to read argv from — but a pin
        already collected in the first pass must still be reported, not swallowed."""
        r = self._check_with_socket(
            _query_pin={"return_value": "opus"},
            _tmux_sessions={"side_effect": [["core"],
                                            subprocess.CalledProcessError(
                                                1, ["tmux"], "", "no server running on /tmp/x.sock")]},
        )
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("opus", r["detail"])

    def test_only_a_successfully_inspected_absence_is_ok(self):
        """Discriminator for both controls: same probe and socket, nothing pinned, but every
        read SUCCEEDS — only this may be ok, or a probe that never returns ok would pass."""
        r = self._check_with_socket(
            _tmux_sessions={"return_value": []},
            _query_pin={"return_value": ""},
        )
        self.assertEqual(r["status"], "ok", r)

    def test_WIRING_check_core_model_pin_actually_inspects_argv(self):
        """Drives the real entry point: the pure-function tests above pass even if
        the collector never calls _core_argv_pins, so this pins the wiring."""
        import os
        import shutil
        import subprocess
        import tempfile
        tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        if not tmux:
            self.fail("tmux unavailable — cannot exercise the wiring")
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, "s.sock")
            stub = os.path.join(td, "claude")
            with open(stub, "w") as fh:
                fh.write("#!/bin/bash\nsleep 120\n")
            os.chmod(stub, 0o755)

            def tm(*a):
                return subprocess.run([tmux, "-S", sock, *a],
                                      capture_output=True, text=True, timeout=15)
            try:
                tm("new-session", "-d", "-s", "sutando-core",
                   f"{stub} --name sutando-core --model opus")
                # tmux env deliberately CLEAN: the pin exists only in the live argv.
                pane = tm("list-panes", "-t", "=sutando-core", "-F", "#{pane_pid}").stdout.strip()
                argv = subprocess.run(["ps", "-o", "args=", "-p", pane],
                                      capture_output=True, text=True).stdout
                if "--model" not in argv:
                    self.fail(f"fixture did not stage a pinned argv: {argv!r}")
                old = os.environ.get("SUTANDO_TMUX_SOCKET")
                os.environ["SUTANDO_TMUX_SOCKET"] = sock
                try:
                    r = self.hc.check_core_model_pin()
                finally:
                    if old is None:
                        os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                    else:
                        os.environ["SUTANDO_TMUX_SOCKET"] = old
                self.assertEqual(r["status"], "warn", r)
                self.assertIn("LIVE core", r["detail"])
            finally:
                tm("kill-server")

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
        # "tmux exploded" carries no no-server marker, so the server may be UP with a live
        # core still pinned; ok is the silent direction because the gate reads status.
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("tmux exploded", r["detail"])
        self.assertIn("could not query", r["detail"])

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
        self.assertIn("could not query", r["detail"], "an uninspected scope must not read as clear")
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
        """A nonzero `list-sessions` must not become an empty session list, which would
        clear the probe without inspecting a single session."""
        import tempfile as _tf
        from unittest import mock
        prev = os.environ.get("SUTANDO_TMUX_SOCKET")
        with _tf.NamedTemporaryFile() as fake_socket:
            os.environ["SUTANDO_TMUX_SOCKET"] = fake_socket.name
            try:
                with mock.patch.object(
                    self.hc.subprocess, "run",
                    return_value=subprocess.CompletedProcess(
                        args=["tmux"], returncode=1, stdout="",
                        stderr="tmux: permission denied"),
                ):
                    r = self.hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not query", r["detail"], "must say it could not query, not that it found nothing")
        self.assertNotIn("no model pin", r["detail"], r)


    def _probe_pinned_core(self, layout):
        """Stage a live `claude --model opus` core with tmux env CLEAN, put it where a
        naive pane scan misses it, and run the real probe."""
        tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        if not tmux:
            self.fail("tmux unavailable — cannot exercise the argv scan")
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, "s.sock")
            stub = os.path.join(td, "claude")
            Path(stub).write_text("#!/bin/bash\nsleep 120\n")
            os.chmod(stub, 0o755)

            def tm(*a):
                return subprocess.run([tmux, "-S", sock, *a],
                                      capture_output=True, text=True, timeout=15)
            try:
                if layout == "core-in-inactive-window":
                    tm("new-session", "-d", "-s", "sutando-core", "-n", "core",
                       f"{stub} --model opus")
                    tm("new-window", "-t", "=sutando-core", "-n", "other", "sleep 120")
                    tm("select-window", "-t", "=sutando-core:other")
                elif layout == "core-in-second-pane":
                    tm("new-session", "-d", "-s", "sutando-core", "-n", "core",
                       "sleep 120")
                    # split-window's -t is a PANE target: "=sutando-core" alone
                    # resolves to no pane and the split silently does not happen.
                    tm("split-window", "-t", "sutando-core:core",
                       f"{stub} --model opus")
                else:
                    self.fail(f"unknown layout {layout!r}")
                self.assertNotIn(
                    "SUTANDO_CORE_MODEL=",
                    tm("show-environment", "-g", "SUTANDO_CORE_MODEL").stdout,
                    "the pin must exist ONLY in the live argv for this to prove anything")
                staged = [subprocess.run(["ps", "-o", "args=", "-p", q],
                                         capture_output=True, text=True).stdout
                          for q in tm("list-panes", "-s", "-t", "=sutando-core",
                                      "-F", "#{pane_pid}").stdout.split()]
                if not any("--model" in a for a in staged):
                    self.fail(f"fixture did not stage a pinned argv: {staged!r}")
                old = os.environ.get("SUTANDO_TMUX_SOCKET")
                os.environ["SUTANDO_TMUX_SOCKET"] = sock
                try:
                    return self.hc.check_core_model_pin()
                finally:
                    if old is None:
                        os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                    else:
                        os.environ["SUTANDO_TMUX_SOCKET"] = old
            finally:
                tm("kill-server")

    def test_pinned_core_in_an_INACTIVE_window_is_still_found(self):
        """tmux list-panes reports only the ACTIVE window unless -s is passed, so a
        core in window 0 vanished whenever any other window was selected."""
        r = self._probe_pinned_core("core-in-inactive-window")
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("opus", r["detail"])
        self.assertNotIn("could not read argv", r["detail"],
                         "the core was readable — it was just in another window")

    def test_pinned_core_in_a_SECOND_pane_is_still_found(self):
        """Reading only the first pane_pid missed a core sharing its window."""
        r = self._probe_pinned_core("core-in-second-pane")
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("opus", r["detail"])

if __name__ == "__main__":
    unittest.main()
