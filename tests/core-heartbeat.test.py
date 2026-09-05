#!/usr/bin/env python3
"""Tests for src/core_heartbeat.py — per-host liveness signal.

Run: python3 tests/core-heartbeat.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _short_host() -> str:
    return socket.gethostname().split(".")[0]


# A stand-in tmux must prove it talked to THIS server: echo the socket and session it was asked about.
_ARGV_PARSE = ('#!/bin/sh\nsock=""; sess=""; prev=""\nfor a in "$@"; do\n  case "$prev" in -S) sock="$a";; -t) sess="${a#=}";; esac\n'
               '  prev="$a"\ndone\n')


EXPORTED_CLIENT = 'case "$*" in\n  *-V*) echo \'tmux 3.5a\';;\n  *has-session*) [ "$sess" = real ] && exit 0 || { echo "no such session: $sess" >&2; exit 1; };;\n  *list-sessions*) case "$*" in *socket_path*) echo "$sock";; *) echo real;; esac;;\n  *list-panes*) [ "$sess" = real ] && echo 4242 || exit 1;;\n  *display-message*) [ "$sess" = real ] && echo "3.5a|$sock|real" || { echo "no such session" >&2; exit 1; };;\n  *) exit 1;;\nesac\n'


def _wait_file(path, deadline_s=5.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            txt = path.read_text()
            if txt.strip():
                return txt
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    return None


class TestHeartbeatWrite(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        # Pin the host label to the short hostname so the .alive filename these
        # tests construct via _short_host() matches what _host_label() resolves
        # to. Without this, on macOS _host_label() prefers scutil LocalHostName
        # (e.g. `Qingyuns-MacBook-Pro-2200`) while _short_host() is the DHCP
        # short name (`QingyunsMBP2200`) — the #1745 drift — and the tests
        # look for the wrong file. CI (Linux, no scutil) already matched; this
        # makes the suite deterministic on drifting hosts too.
        self._saved_label = os.environ.get("SUTANDO_HOST_LABEL")
        os.environ["SUTANDO_HOST_LABEL"] = _short_host()
        self.tmp = Path(tempfile.mkdtemp(prefix="core-heartbeat-"))
        os.environ["SUTANDO_WORKSPACE"] = str(self.tmp)
        os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
        # Force re-import so module picks up the new env.
        sys.modules.pop("core_heartbeat", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        if self._saved_label is not None:
            os.environ["SUTANDO_HOST_LABEL"] = self._saved_label
        else:
            os.environ.pop("SUTANDO_HOST_LABEL", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        sys.modules.pop("core_heartbeat", None)

    def test_write_beat_creates_per_host_file(self):
        import core_heartbeat
        core_heartbeat.write_beat()
        alive_path = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        self.assertTrue(alive_path.is_file(), f"expected {alive_path} to exist")

    def test_handle_signal_writes_tombstone_before_unlink(self):
        import core_heartbeat
        core_heartbeat.write_beat()
        alive = core_heartbeat._alive_path()
        self.assertTrue(alive.is_file())
        core_heartbeat._handle_signal(15, None)
        stopped = alive.with_suffix(".stopped")
        self.assertTrue(stopped.is_file(), "graceful stop must leave a .stopped tombstone (#2160)")
        self.assertFalse(alive.exists(), "graceful stop must still unlink .alive")
        float(stopped.read_text())  # payload is a timestamp

    def test_write_beat_payload_schema(self):
        import core_heartbeat
        # Pin the core-pid resolver. Before schema 3 this test asserted
        # `pid == os.getpid()` and passed only because CI runners have no tmux
        # server on the socket — on a machine that DID, it would have failed.
        # Pinning makes the contract, not the environment, decide.
        _orig = core_heartbeat.core_pid
        core_heartbeat.core_pid = lambda socket_path=None, session=None: 4242
        try:
            core_heartbeat.write_beat(status="custom-status")
        finally:
            core_heartbeat.core_pid = _orig
        data = json.loads((self.tmp / "state" / "cores" / f"{_short_host()}.alive").read_text())
        # Required fields
        self.assertEqual(data["host"], _short_host())
        # schema 3: `pid` is the CORE's (what the docstring always claimed);
        # the writer's own pid moved to `heartbeat_pid`. Before this, a dead
        # core read as healthy because the writer outlives core restarts.
        self.assertEqual(data["pid"], 4242)
        self.assertEqual(data["heartbeat_pid"], os.getpid())
        self.assertNotEqual(data["pid"], data["heartbeat_pid"])
        self.assertEqual(data["status"], "custom-status")
        self.assertEqual(data["schema_version"], 4)
        # schema 4: a client VERIFIED to speak to this server (not its creator) and the
        # server's own version, so a reader starts from a compatible binary.
        self.assertEqual(data["backend"], "tmux")
        for k in ("tmux_binary", "tmux_version", "tmux_server_version", "tmux_verified", "tmux_candidates"):
            self.assertIn(k, data)
        # locality (Track 10): {kind, host}, self-reported. Default kind=local.
        self.assertEqual(data["locality"], {"kind": "local", "host": _short_host()})
        # session: what tmux says this core is IN, not what the env claims.
        self.assertEqual(data["session"], core_heartbeat._observed_session(
            os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")))

        # socket: the runtime-authored tmux socket the core runs on. Consumed by
        # `sutando-config.sh runtime` so the AgentRuntime descriptor reports the
        # real socket (incl. custom sockets) independent of a caller's env.
        self.assertEqual(
            data["socket"],
            os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock"),
        )
        self.assertIsInstance(data["started_at"], float)
        self.assertIsInstance(data["last_beat_at"], float)
        # last_beat_at advances after a sleep; just sanity-check it's recent.
        self.assertLess(abs(time.time() - data["last_beat_at"]), 5)

    def test_observed_session_prefers_tmux_over_a_lying_env(self):
        """The Claude launcher hardcodes SESSION and never forwards
        SUTANDO_TMUX_SESSION, so an exported value can name a session that does
        not exist. Recording it would send the owner to a dead target."""
        import core_heartbeat
        import subprocess as _sp
        _orig = core_heartbeat._tmux
        core_heartbeat._tmux = lambda sock, *a: _sp.CompletedProcess(
            a, 0, stdout="sutando-core\n", stderr="")
        os.environ["TMUX"] = "/tmp/sutando-tmux.sock,1,0"
        os.environ["SUTANDO_TMUX_SESSION"] = "custom-core-does-not-exist"
        try:
            self.assertEqual(core_heartbeat.core_session(),
                             "custom-core-does-not-exist")   # the env still lies
            self.assertEqual(core_heartbeat._observed_session("/tmp/s.sock"),
                             "sutando-core")                 # tmux wins
        finally:
            core_heartbeat._tmux = _orig
            os.environ.pop("SUTANDO_TMUX_SESSION", None)
            os.environ.pop("TMUX", None)

    def test_observed_session_never_uses_bare_display_message_outside_tmux(self):
        """Outside tmux a bare display-message resolves an arbitrary session on a
        shared socket. Scoped calls are fine; that one specific call is not."""
        import core_heartbeat
        import subprocess as _sp
        _orig, _origpid = core_heartbeat._tmux, core_heartbeat.core_pid

        def _guard(sock, *a):
            if a and a[0] == "display-message":
                raise AssertionError("bare display-message outside tmux")
            return _sp.CompletedProcess(["tmux"], 1, "", "")

        core_heartbeat._tmux = _guard
        core_heartbeat.core_pid = lambda socket_path=None, session=None: None
        os.environ.pop("TMUX", None)
        os.environ["SUTANDO_TMUX_SESSION"] = "from-contract"
        try:
            self.assertEqual(core_heartbeat._observed_session("/tmp/s.sock"),
                             "from-contract")
        finally:
            core_heartbeat._tmux, core_heartbeat.core_pid = _orig, _origpid
            os.environ.pop("SUTANDO_TMUX_SESSION", None)

    def test_write_beat_records_the_live_session_not_a_lying_env(self):
        """With $TMUX unset and SUTANDO_TMUX_SESSION naming a session that does not
        exist, the beat records the live session on the socket, not the env's claim."""
        import core_heartbeat
        import json as _json
        import subprocess as _sp
        _orig, _origpid = core_heartbeat._tmux, core_heartbeat.core_pid
        LIE, REAL = "custom-core-does-not-exist", "sutando-core"

        def _fake_tmux(sock, *a):
            if a and a[0] == "list-sessions":
                return _sp.CompletedProcess(["tmux"], 0, f"{REAL}\n", "")
            return _sp.CompletedProcess(["tmux"], 1, "", "")

        core_heartbeat._tmux = _fake_tmux
        core_heartbeat.core_pid = (lambda socket_path=None, session=None:
                                  4321 if session == REAL else None)
        os.environ.pop("TMUX", None)
        os.environ["SUTANDO_TMUX_SESSION"] = LIE
        try:
            core_heartbeat.write_beat()
            rec = _json.loads((self.tmp / "state" / "cores" /
                               f"{_short_host()}.alive").read_text())
            self.assertNotEqual(rec["session"], LIE,
                                "recorded the unverified env value")
            self.assertEqual(rec["session"], REAL)
        finally:
            core_heartbeat._tmux, core_heartbeat.core_pid = _orig, _origpid
            os.environ.pop("SUTANDO_TMUX_SESSION", None)

    def test_locality_kind_from_env(self):
        """Track 10: `kind` self-reports from $SUTANDO_CORE_LOCALITY — `cloud`
        for the spawn-user-core template, defaulting to `local` for a normal
        launch, and clamping any unrecognized value back to `local`."""
        import core_heartbeat
        path = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        saved = os.environ.get("SUTANDO_CORE_LOCALITY")
        try:
            # cloud: explicit template value
            os.environ["SUTANDO_CORE_LOCALITY"] = "cloud"
            core_heartbeat.write_beat()
            self.assertEqual(json.loads(path.read_text())["locality"]["kind"], "cloud")
            # case/whitespace tolerant
            os.environ["SUTANDO_CORE_LOCALITY"] = "  Cloud  "
            core_heartbeat.write_beat()
            self.assertEqual(json.loads(path.read_text())["locality"]["kind"], "cloud")
            # unrecognized value clamps to local (fail toward the safe case)
            os.environ["SUTANDO_CORE_LOCALITY"] = "bogus"
            core_heartbeat.write_beat()
            self.assertEqual(json.loads(path.read_text())["locality"]["kind"], "local")
            # unset → local
            del os.environ["SUTANDO_CORE_LOCALITY"]
            core_heartbeat.write_beat()
            self.assertEqual(json.loads(path.read_text())["locality"]["kind"], "local")
        finally:
            if saved is not None:
                os.environ["SUTANDO_CORE_LOCALITY"] = saved
            else:
                os.environ.pop("SUTANDO_CORE_LOCALITY", None)

    def _fake_tmux(self, name, speaks, version="3.6b"):
        # display-message succeeds only when `speaks`; -V always answers.
        f = self.tmp / name
        refuse = "echo 'protocol version mismatch (client 8, server 9)' >&2; exit 1"
        body = (_ARGV_PARSE + "case \"$*\" in\n  *-V*) echo 'tmux %s';;\n  *list-sessions*) %s;;\n  *display-message*) %s;;\n  *) exit 1;;\nesac\n") % (
            version, ('echo "$sock"' if speaks else refuse), (('echo "%s|$sock|$sess"' % version) if speaks else refuse))
        f.write_text(body); f.chmod(0o755); return str(f)

    def test_tmux_backend_records_a_client_verified_against_the_socket(self):
        import core_heartbeat
        path_tmux = self._fake_tmux("tmux", speaks=True, version="3.6b")
        exported = self._fake_tmux("exported-tmux", speaks=False, version="3.5a")
        # The mixed case: the app exported one binary, the launcher ran the PATH one.
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": exported, "PATH": str(self.tmp)}):
            b = core_heartbeat._tmux_backend(sock="/tmp/x.sock", sess="sutando-core")
        self.assertEqual((b["tmux_binary"], b["tmux_version"], b["tmux_server_version"], b["tmux_verified"]),
                         (path_tmux, "3.6b", "3.6b", True))
        self.assertEqual(b["tmux_candidates"], [path_tmux, exported])
        # the other way round: only the exported binary speaks → it is the record
        path_tmux2 = self._fake_tmux("tmux", speaks=False, version="3.6b")
        exported2 = self._fake_tmux("exported-tmux", speaks=True, version="3.5a")
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": exported2, "PATH": str(self.tmp)}):
            b2 = core_heartbeat._tmux_backend(sock="/tmp/x.sock", sess="sutando-core")
        self.assertEqual((b2["tmux_binary"], b2["tmux_version"], b2["tmux_server_version"]), (exported2, "3.5a", "3.5a"))
        # nothing speaks → nulls and verified False, never a guess
        self._fake_tmux("tmux", speaks=False); self._fake_tmux("exported-tmux", speaks=False)
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(self.tmp / "exported-tmux"), "PATH": str(self.tmp)}):
            b3 = core_heartbeat._tmux_backend(sock="/tmp/x.sock", sess="sutando-core")
        self.assertEqual((b3["tmux_binary"], b3["tmux_version"], b3["tmux_server_version"], b3["tmux_verified"]), (None, None, None, False))

    def test_tmux_backend_verifies_the_observed_session_not_the_env_claim(self):
        # The verifier must use the OBSERVED session write_beat records, not the env's claim:
        # a lying SUTANDO_TMUX_SESSION left the payload unverified with a compatible client present.
        import core_heartbeat
        f = self.tmp / "tmux"
        f.write_text(_ARGV_PARSE + "case \"$*\" in\n  *-V*) echo 'tmux 3.6b';;\n  *'-t =real'*) echo \"3.6b|$sock|real\";;\n  *) echo 'no such session' >&2; exit 1;;\nesac\n")
        f.chmod(0o755)
        _obs, _pid = core_heartbeat._observed_session, core_heartbeat.core_pid
        core_heartbeat._observed_session = lambda sock: "real"
        core_heartbeat.core_pid = lambda socket_path=None, session=None: 4242
        try:
            with patch.dict(os.environ, {"SUTANDO_TMUX_SESSION": "lie", "SUTANDO_TMUX_BIN": str(f), "PATH": str(self.tmp)}):
                core_heartbeat.write_beat()
        finally:
            core_heartbeat._observed_session, core_heartbeat.core_pid = _obs, _pid
        data = json.loads((self.tmp / "state" / "cores" / f"{_short_host()}.alive").read_text())
        self.assertEqual((data["session"], data["tmux_verified"], data["tmux_binary"], data["tmux_server_version"]), ("real", True, str(f), "3.6b"))

    def test_tmux_backend_is_reverified_every_beat_so_a_replaced_server_is_seen(self):
        # A memoized failure would make a cold-boot miss permanent and a memoized success would
        # survive a server replacement; nothing is cached — every call asks the live server.
        import core_heartbeat
        f = self.tmp / "tmux"
        ver = self.tmp / "server-version"
        # PATH is pinned to the temp dir below, so the stand-in must use absolute tool paths.
        f.write_text(_ARGV_PARSE + "case \"$*\" in\n  *-V*) echo \"tmux $(/bin/cat '%s' 2>/dev/null || echo none)\";;\n"
                     "  *display-message*) [ -s '%s' ] && echo \"$(/bin/cat '%s')|$sock|$sess\" || { echo 'no server running' >&2; exit 1; };;\n"
                     "  *) exit 1;;\nesac\n" % (ver, ver, ver))
        f.chmod(0o755)
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(f), "PATH": str(self.tmp)}):
            first = core_heartbeat._tmux_backend(sock="/tmp/x", sess="s")        # cold boot: no server yet
            self.assertFalse(first["tmux_verified"])
            ver.write_text("3.5a")
            second = core_heartbeat._tmux_backend(sock="/tmp/x", sess="s")       # server up → heals, no refresh needed
            self.assertEqual((second["tmux_verified"], second["tmux_server_version"]), (True, "3.5a"))
            ver.write_text("3.6b")                                                # server replaced between beats
            third = core_heartbeat._tmux_backend(sock="/tmp/x", sess="s")
            self.assertEqual(third["tmux_server_version"], "3.6b")

    def test_tmux_backend_tolerates_a_missing_or_vanishing_binary(self):
        # A candidate that cannot be executed is skipped (not raised); a binary that speaks
        # and then disappears before -V still yields the verified client with version None.
        import core_heartbeat
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(self.tmp / "absent"), "PATH": str(self.tmp / "empty")}):
            b = core_heartbeat._tmux_backend(sock="/tmp/x", sess="s")
        self.assertEqual((b["tmux_verified"], b["tmux_candidates"]), (False, [str(self.tmp / "absent")]))
        f = self.tmp / "tmux"
        f.write_text(_ARGV_PARSE + "case \"$*\" in\n  *display-message*) echo \"3.6b|$sock|$sess\"; /bin/rm -f \"$0\";;\n  *) exit 1;;\nesac\n")
        f.chmod(0o755)
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(f), "PATH": str(self.tmp / "empty")}):
            b2 = core_heartbeat._tmux_backend(sock="/tmp/x", sess="s")
        self.assertEqual((b2["tmux_verified"], b2["tmux_binary"], b2["tmux_server_version"], b2["tmux_version"]), (True, str(f), "3.6b", None))

    def test_tmux_backend_rejects_a_runnable_wrong_binary_and_malformed_success(self):
        # Exit 0 with output is not "verified": /bin/echo answers anything. The proof is the server's
        # own socket path and the session name coming back; a -V that is not tmux records no version.
        import core_heartbeat
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": "/bin/echo", "PATH": str(self.tmp / "empty")}):
            b = core_heartbeat._tmux_backend(sock="/tmp/definitely-no-server.sock", sess="nope")
        self.assertEqual((b["tmux_binary"], b["tmux_version"], b["tmux_server_version"], b["tmux_verified"]),
                         (None, None, None, False), b)
        for name, out in (("bare-version", "echo '3.6b'"), ("other-socket", "echo \"3.6b|/tmp/other.sock|$sess\""),
                          ("other-session", "echo \"3.6b|$sock|someone-else\"")):
            f = self.tmp / name
            f.write_text(_ARGV_PARSE + "case \"$*\" in\n  *-V*) echo 'tmux 3.6b';;\n  *display-message*) %s;;\n  *) exit 1;;\nesac\n" % out)
            f.chmod(0o755)
            with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(f), "PATH": str(self.tmp / "empty")}):
                b = core_heartbeat._tmux_backend(sock="/tmp/x.sock", sess="core")
            self.assertFalse(b["tmux_verified"], (name, b))
        g = self.tmp / "odd-version"   # speaks, but its -V is not a tmux banner
        g.write_text(_ARGV_PARSE + "case \"$*\" in\n  *-V*) echo 'something 9.9';;\n  *display-message*) echo \"3.6b|$sock|$sess\";;\n  *) exit 1;;\nesac\n")
        g.chmod(0o755)
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(g), "PATH": str(self.tmp / "empty")}):
            b = core_heartbeat._tmux_backend(sock="/tmp/x.sock", sess="core")
        self.assertEqual((b["tmux_verified"], b["tmux_server_version"], b["tmux_version"]), (True, "3.6b", None))

    def test_unpatched_beat_discovers_the_session_through_the_compatible_client(self):
        # PATH tmux is protocol-refused, the exported client speaks, the env names a session that does
        # not exist: the FULL write_beat (no seams patched) must find `real` through the exported client.
        import core_heartbeat
        bindir = self.tmp / "bin"; bindir.mkdir()
        (bindir / "tmux").write_text("#!/bin/sh\necho 'protocol version mismatch (client 8, server 9)' >&2; exit 1\n")
        (bindir / "ps").write_text("#!/bin/sh\necho 'claude --name real --resume'\n")
        (bindir / "pgrep").write_text("#!/bin/sh\necho 4242\n")
        exported = self.tmp / "exported-tmux"
        exported.write_text(_ARGV_PARSE + EXPORTED_CLIENT)
        for f in (bindir / "tmux", bindir / "ps", bindir / "pgrep", exported):
            f.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(bindir), "SUTANDO_TMUX_BIN": str(exported),
                                     "SUTANDO_TMUX_SESSION": "lie", "SUTANDO_TMUX_SOCKET": "/tmp/unpatched.sock"}):
            core_heartbeat._CLIENT_CACHE.clear()
            core_heartbeat.write_beat()
        data = json.loads((self.tmp / "state" / "cores" / f"{_short_host()}.alive").read_text())
        self.assertEqual((data["session"], data["tmux_binary"], data["tmux_verified"], data["tmux_server_version"], data["pid"], data["socket"]),
                         ("real", str(exported), True, "3.5a", 4242, "/tmp/unpatched.sock"), data)

    def test_write_beat_is_a_noop_once_the_signal_handler_ran(self):
        # The handler unlinks .alive; a beat already in flight must not republish it.
        import core_heartbeat
        core_heartbeat._SIGNALLED = True
        try:
            core_heartbeat.write_beat()
        finally:
            core_heartbeat._SIGNALLED = False
        self.assertFalse((self.tmp / "state" / "cores" / f"{_short_host()}.alive").exists())

    def test_restart_paths_hand_the_heartbeat_over(self):
        # Both launchers stop the running writer before starting one; startup only starts when none runs.
        restart = (ROOT / "src" / "restart.sh").read_text()
        codex = (ROOT / "src" / "agent" / "codex" / "cli" / "start-cli.sh").read_text()
        self.assertIn('core_heartbeat.py" --stop', restart)
        self.assertIn('core_heartbeat.py" --stop', codex)
        self.assertLess(codex.index('core_heartbeat.py" --stop'), codex.index("  ensure_core_heartbeat\n"))

    def test_stop_other_writers_in_process_ends_a_stand_in_and_reports_it(self):
        # Coverage runs in-process: the handoff itself, not only its CLI wrapper, must be exercised here.
        import core_heartbeat
        script = str(Path(core_heartbeat.__file__).resolve())
        # Selection is from the RECORD, identity from argv: a recorded pid that is not a writer is left alone.
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", script, "not-a-writer"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        quiet = {**os.environ, "SUTANDO_TMUX_SOCKET": str(self.tmp / "no-server.sock")}   # no core → publishes nothing
        old = subprocess.Popen([sys.executable, script, "--interval", "60"], env=quiet, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertIsNotNone(_wait_file(core_heartbeat._pidfile()), "the writer did not record its pid")
            core_heartbeat._pidfile().write_text(f"{bystander.pid} {script}\n")          # a record naming a non-writer
            self.assertEqual(core_heartbeat.stop_other_writers(timeout_s=2.0), 0)
            self.assertIsNone(bystander.poll())
            core_heartbeat._pidfile().write_text(f"{old.pid} {script}\n")                # the real writer's record
            self.assertTrue(core_heartbeat._pid_running(old.pid))
            self.assertEqual(core_heartbeat.stop_other_writers(timeout_s=5.0), 1)
            self.assertIsNotNone(old.wait(timeout=5))
            self.assertIsNone(bystander.poll())
            self.assertFalse(core_heartbeat._is_writer_argv(f"python3 -c import time; time.sleep(60) {script} not-a-writer", script))
            self.assertTrue(core_heartbeat._is_writer_argv(f"/opt/py/bin/python3.12 {script} --interval 60", script))
            self.assertFalse(core_heartbeat._is_writer_argv(f"bash {script}", script))
            self.assertFalse(core_heartbeat._is_writer_argv(f"python3 {script}x", script))
        finally:
            for pr in (old, bystander):
                if pr.poll() is None:
                    pr.kill(); pr.wait()
        self.assertFalse(core_heartbeat._pid_running(old.pid))   # reaped: lookup fails or state is Z
        self.assertTrue(core_heartbeat._pid_running(os.getpid()))
        with patch("core_heartbeat.os.kill", side_effect=PermissionError):
            self.assertTrue(core_heartbeat._pid_running(1))       # not ours to signal, but running
        with patch("core_heartbeat.subprocess.run", side_effect=OSError("no ps")):
            self.assertTrue(core_heartbeat._pid_running(os.getpid()))   # unknown state reads as running
        # main(--stop) with nothing to stop still answers, and never raises
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = core_heartbeat.main(["--stop"])
        self.assertEqual(rc, 0)
        self.assertIn("core_heartbeat: stopped", buf.getvalue())
        core_heartbeat._pidfile().write_text(f"{os.getpid()} {script}\n")   # a record naming THIS process is skipped
        with patch("core_heartbeat.subprocess.run", side_effect=OSError("no ps")):
            self.assertEqual(core_heartbeat.stop_other_writers(), 0)
        core_heartbeat._pidfile().write_text(f"424242 {script}\n")
        with patch("core_heartbeat.subprocess.run", side_effect=OSError("no ps")):
            self.assertEqual(core_heartbeat.stop_other_writers(), 0)              # cannot verify → left alone

    def test_handoff_edge_paths_are_covered_in_process(self):
        # Records: an absent pidfile and a .alive naming a heartbeat_pid; argv with no script at all;
        # a pid that vanishes between ps and SIGTERM; a pid that ignores SIGTERM and takes the SIGKILL path.
        import core_heartbeat
        script = str(Path(core_heartbeat.__file__).resolve())
        cores = self.tmp / "state" / "cores"; cores.mkdir(parents=True, exist_ok=True)
        core_heartbeat._pidfile().unlink(missing_ok=True)
        (cores / f"{_short_host()}.alive").write_text(json.dumps({"heartbeat_pid": 777001}))
        self.assertEqual(core_heartbeat._recorded_writer_pids(), [777001])
        (cores / f"{_short_host()}.alive").write_text("not json")
        self.assertEqual(core_heartbeat._recorded_writer_pids(), [])
        self.assertFalse(core_heartbeat._is_writer_argv("bash -c sleep 60", script))
        quiet = {**os.environ, "SUTANDO_TMUX_SOCKET": str(self.tmp / "no-server.sock")}
        old = subprocess.Popen([sys.executable, script, "--interval", "60"], env=quiet, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertIsNotNone(_wait_file(core_heartbeat._pidfile()))
            # vanished between ps and SIGTERM: counted as handled, nothing raised
            with patch("core_heartbeat.os.kill", side_effect=ProcessLookupError), patch("core_heartbeat._pid_running", return_value=False):
                self.assertEqual(core_heartbeat.stop_other_writers(timeout_s=0.5), 1)
            self.assertIsNone(old.poll())      # the patch swallowed the signal; the writer is still there
            # ignores SIGTERM (simulated by _pid_running staying True): the SIGKILL path runs and ends it
            with patch("core_heartbeat._pid_running", return_value=True):
                self.assertEqual(core_heartbeat.stop_other_writers(timeout_s=0.3), 1)
            self.assertIsNotNone(old.wait(timeout=5))
            # SIGKILL on a pid that is already gone is not an error
            core_heartbeat._pidfile().write_text(f"{old.pid} {script}\n")
            with patch("core_heartbeat.subprocess.run", return_value=subprocess.CompletedProcess([], 0, f"python3 {script} --interval 60", "")), \
                 patch("core_heartbeat._pid_running", return_value=True), \
                 patch("core_heartbeat.os.kill", side_effect=[None, ProcessLookupError]):
                self.assertEqual(core_heartbeat.stop_other_writers(timeout_s=0.2), 1)
        finally:
            if old.poll() is None:
                old.kill(); old.wait()

    def test_client_and_socket_helpers_fail_closed_on_errors(self):
        import core_heartbeat
        # a candidate that cannot be executed is skipped, not raised
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(self.tmp / "absent-tmux"), "PATH": str(self.tmp / "empty")}):
            core_heartbeat._CLIENT_CACHE.clear()
            self.assertIsNone(core_heartbeat._client_for("/tmp/nope.sock"))
        # a non-path reported socket is not the socket
        self.assertFalse(core_heartbeat._same_socket(123, "/tmp/x.sock"))  # type: ignore[arg-type]
        self.assertFalse(core_heartbeat._same_socket("", "/tmp/x.sock"))
        self.assertTrue(core_heartbeat._same_socket("/tmp/x.sock", "/tmp/x.sock"))
        # a signalled exit unlinks .alive once more after the loop
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        alive.parent.mkdir(parents=True, exist_ok=True); alive.write_text("{}")
        core_heartbeat._SIGNALLED = True; core_heartbeat._SHUTDOWN_REQUESTED = True
        try:
            self.assertEqual(core_heartbeat.run_forever(interval=0.01), 0)
        finally:
            core_heartbeat._SIGNALLED = False; core_heartbeat._SHUTDOWN_REQUESTED = False
        self.assertFalse(alive.exists())

    def test_write_beat_is_atomic_via_tmp(self):
        """The .alive write goes through .alive.tmp then renames into place —
        a concurrent reader at the destination path never sees a half-file."""
        import core_heartbeat
        core_heartbeat.write_beat()
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        tmp = self.tmp / "state" / "cores" / f"{_short_host()}.alive.tmp"
        self.assertTrue(alive.exists())
        self.assertFalse(tmp.exists(), "tmp file should have been renamed away")

    def test_write_beat_overwrites_on_second_call(self):
        import core_heartbeat
        core_heartbeat.write_beat(status="first")
        path = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        first_data = json.loads(path.read_text())
        time.sleep(0.01)
        core_heartbeat.write_beat(status="second")
        second_data = json.loads(path.read_text())
        self.assertEqual(second_data["status"], "second")
        # started_at should NOT change — it's set at module import.
        self.assertEqual(first_data["started_at"], second_data["started_at"])
        # last_beat_at should advance.
        self.assertGreater(second_data["last_beat_at"], first_data["last_beat_at"])

    def test_write_beat_creates_cores_dir(self):
        """The cores/ dir must be created if it doesn't yet exist — fresh
        install case."""
        import core_heartbeat
        cores_dir = self.tmp / "state" / "cores"
        self.assertFalse(cores_dir.exists())
        core_heartbeat.write_beat()
        self.assertTrue(cores_dir.is_dir())


class TestHeartbeatCli(unittest.TestCase):
    """End-to-end tests that exercise the script via subprocess so the CLI
    parsing, signal handling, and cleanup paths are covered."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="core-heartbeat-cli-"))
        # SUTANDO_TEST_MODE: post-v0.8 the resolver ignores $SUTANDO_WORKSPACE
        # unless this test-only escape hatch is set (mirrors line 30 in the
        # in-process fixture above — the subprocess env doesn't inherit it).
        # Pin SUTANDO_HOST_LABEL into the SUBPROCESS env for the same reason as
        # the in-process fixture (line 36): the child's _host_label() prefers
        # scutil LocalHostName on macOS, so without this the child writes
        # `<scutil-label>.alive` while these tests assert `<short-host>.alive`
        # — the #1745 drift, and both CLI cases fail locally. CI/Linux (no
        # scutil) matched already; this makes the subprocess path deterministic
        # on drifting hosts too.
        self.env = {**os.environ, "SUTANDO_WORKSPACE": str(self.tmp),
                    "SUTANDO_TEST_MODE": "1",
                    "SUTANDO_HOST_LABEL": _short_host()}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_once_flag_writes_single_beat_and_exits(self):
        script = ROOT / "src" / "core_heartbeat.py"
        result = subprocess.run(
            [sys.executable, str(script), "--once", "--status", "smoke"],
            env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        self.assertTrue(alive.is_file())
        data = json.loads(alive.read_text())
        self.assertEqual(data["status"], "smoke")

    def test_stop_hands_over_from_an_old_writer_to_a_schema_4_singleton(self):
        # An old writer (argv names this checkout's script) is still running; --stop must end it and
        # wait, then a fresh --once beat carries schema 4. No pkill pattern kills another checkout's.
        script = ROOT / "src" / "core_heartbeat.py"
        # A REAL old writer (it records its own pid before its first beat), plus a bystander whose argv
        # merely mentions the script path — the shape a pgrep sweep would have killed.
        old = subprocess.Popen([sys.executable, str(script), "--interval", "60"],
                               env={**self.env, "SUTANDO_TMUX_SOCKET": str(self.tmp / "no-server.sock")},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(script.resolve()), "not-a-writer"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            pidfile = self.tmp / "state" / "cores" / f"{_short_host()}.heartbeat.pid"
            self.assertIsNotNone(_wait_file(pidfile), "the writer did not record its pid")
            r = subprocess.run([sys.executable, str(script), "--stop"], env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("stopped 1 writer(s)", r.stdout)
            self.assertIsNotNone(old.wait(timeout=5), "the old writer must be gone before --stop returns")
            time.sleep(0.2)
            self.assertIsNone(bystander.poll(), "a process that only mentions the path must not be touched")
        finally:
            for pr in (old, bystander):
                if pr.poll() is None:
                    pr.kill(); pr.wait()
        r2 = subprocess.run([sys.executable, str(script), "--once"], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        data = json.loads((self.tmp / "state" / "cores" / f"{_short_host()}.alive").read_text())
        self.assertEqual(data["schema_version"], 4)

    def test_sigterm_cleans_up_alive_file(self):
        """Graceful shutdown removes the .alive file so peers see the core
        leave immediately rather than wait for mtime staleness."""
        import signal as _signal
        script = ROOT / "src" / "core_heartbeat.py"
        # A beat is only published AFTER a core has been observed (4fda4a4b:
        # before first sighting the loop publishes nothing and unlinks any stale
        # record, so a cold boot that never gets a core cannot advertise itself
        # healthy). This test is about SIGTERM cleanup, and it needs a file to
        # clean up — so give the child a toolchain that reports a real core
        # instead of asserting the pre-4fda4a4b fail-open behaviour.
        _bin = self.tmp / "fakebin"
        _bin.mkdir(parents=True, exist_ok=True)
        for _name, _body in (
            ("tmux",  "#!/bin/sh\nexit 0\n"),
            ("pgrep", "#!/bin/sh\necho 4242\n"),
            ("ps",    "#!/bin/sh\necho 'claude --name sutando-core --resume'\n"),
        ):
            _f = _bin / _name
            _f.write_text(_body)
            _f.chmod(0o755)
        _env = dict(self.env)
        _env["PATH"] = f"{_bin}:{_env.get('PATH', '')}"
        proc = subprocess.Popen(
            [sys.executable, str(script), "--interval", "0.5"],
            env=_env,
        )
        # Wait for first beat to land.
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        for _ in range(40):
            if alive.exists():
                break
            time.sleep(0.1)
        self.assertTrue(alive.exists(),
                        "first beat should have landed within 4s once a core is observed")
        # Signal graceful shutdown.
        proc.send_signal(_signal.SIGTERM)
        proc.wait(timeout=5)
        self.assertFalse(alive.exists(), ".alive should have been unlinked on SIGTERM")


if __name__ == "__main__":
    unittest.main(verbosity=2)
