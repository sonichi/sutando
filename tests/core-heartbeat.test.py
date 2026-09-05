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
        # schema 4: the tmux that created the server, so a client can start from the
        # same binary — a version mismatch otherwise reads a live core as absent.
        self.assertEqual(data["backend"], "tmux")
        self.assertIn("tmux_binary", data)
        self.assertIn("tmux_version", data)
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

    def test_tmux_backend_records_the_launcher_binary_and_its_version(self):
        import core_heartbeat
        fake = self.tmp / "tmux"
        fake.write_text("#!/bin/sh\necho 'tmux 3.6b'\n")
        fake.chmod(0o755)
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(fake)}):
            b = core_heartbeat._tmux_backend(refresh=True)
        self.assertEqual((b["backend"], b["tmux_binary"], b["tmux_version"]), ("tmux", str(fake), "3.6b"))
        # a binary that cannot run leaves the version None, never raises
        with patch.dict(os.environ, {"SUTANDO_TMUX_BIN": str(self.tmp / "missing")}):
            b2 = core_heartbeat._tmux_backend(refresh=True)
        self.assertEqual((b2["tmux_binary"], b2["tmux_version"]), (str(self.tmp / "missing"), None))
        core_heartbeat._tmux_backend(refresh=True)  # back to this host's real answer

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
