#!/usr/bin/env python3
"""Every consumer of the default runtime socket must resolve the SAME path.

The daemon, the canonical CLI and the shell descriptor each derived the socket
themselves. The daemon added the actor (enrolled record, else the `local-agent`
fallback) while the CLI knew only environment variables and the shell still
published the pre-actor flat socket, so with nothing overridden a fresh daemon
listened on a path no client asked for.

These controls therefore run each consumer THE WAY IT SHIPS — the daemon
through `build_runtime_server()`, the CLI through the module `bin/sutando`
execs, the shell through `scripts/sutando-config.sh` — with
SUTANDO_RUNTIME_SOCKET UNSET, which is precisely what the existing suites
never did: `runtime-rundir-resolver.test.sh` pinned only the shell copies and
`runtime-api-e2e.test.py` sets the override explicitly.

Run: python3 tests/runtime-api-locator.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import rundir  # noqa: E402

SERVER = ROOT / "src" / "runtime-api" / "server.py"
CLI = ROOT / "src" / "runtime-cli" / "sutando-runtime.py"
CONFIG_SH = ROOT / "scripts" / "sutando-config.sh"
# Overrides that would answer the question for us — every control clears them.
_CLEARED = ("SUTANDO_RUNTIME_SOCKET", "SUTANDO_AGENT_ID", "AGENT_MXID",
            "AGENT_ID", "SUTANDO_INSTANCE_ID")


def _run(argv, env, timeout=60):
    return subprocess.run(argv, env=env, capture_output=True, text=True,
                          timeout=timeout, cwd=str(ROOT))


class LocatorAgreement(unittest.TestCase):
    def setUp(self):
        # /tmp, not TMPDIR: macOS TMPDIR is long enough to eat the sun_path cap.
        self.base = Path(tempfile.mkdtemp(prefix="loc-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.state = self.base / "state"
        self.state.mkdir(parents=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in _CLEARED}
        env.update({"SUTANDO_RUN_DIR": str(self.base / "run"),
                    "SUTANDO_RUNTIME_STATE": str(self.state),
                    "SUTANDO_INSTANCE_REGISTRY": str(self.base / "instances"),
                    "SUTANDO_RUNTIME_DB": str(self.base / "rt.sqlite"),
                    "SUTANDO_HA_DIR": str(self.base / "ha")})
        env.update(extra)
        return env

    def enroll(self, agent_id):
        auth = self.state / "auth"
        auth.mkdir(parents=True, exist_ok=True)
        (auth / "ag2space.json").write_text(json.dumps({"agent_id": agent_id}))

    # ── the three shipped consumers ────────────────────────────────────────
    def daemon_socket(self, env):
        # Own DB file: composing a probe server must not contend with a live
        # daemon's WAL lock, which is a fixture artifact, not the property here.
        probe = {**env, "SUTANDO_RUNTIME_DB": str(self.base / "probe.sqlite")}
        out = _run([sys.executable, "-c",
                    f"import sys; sys.path.insert(0, {str(SERVER.parent)!r});"
                    " import server;"
                    " print(server.build_runtime_server().socket_path)"], probe)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def cli_socket(self, env):
        out = _run([sys.executable, "-c",
                    "import importlib.util as u;"
                    f" s=u.spec_from_file_location('rtcli', {str(CLI)!r});"
                    " m=u.module_from_spec(s); s.loader.exec_module(m);"
                    " print(m._socket_path())"], env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def shell_socket(self, env):
        out = _run(["bash", str(CONFIG_SH), "runtime-socket"], env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def all_three(self, env):
        return (self.daemon_socket(env), self.cli_socket(env),
                self.shell_socket(env))

    def all_three_rc(self, env):
        """Exit statuses, not values — the negative controls need the failure
        mode, which the value-returning helpers assert away."""
        probe = {**env, "SUTANDO_RUNTIME_DB": str(self.base / "probe.sqlite")}
        return (
            _run([sys.executable, "-c",
                  f"import sys; sys.path.insert(0, {str(SERVER.parent)!r});"
                  " import server;"
                  " print(server.build_runtime_server().socket_path)"], probe),
            _run([sys.executable, "-c",
                  "import importlib.util as u;"
                  f" s=u.spec_from_file_location('rtcli', {str(CLI)!r});"
                  " m=u.module_from_spec(s); s.loader.exec_module(m);"
                  " print(m._socket_path())"], env),
            _run(["bash", str(CONFIG_SH), "runtime-socket"], env))

    def test_unenrolled_daemon_cli_and_shell_agree(self):
        got = self.all_three(self.env())
        self.assertEqual(len(set(got)), 1, f"resolvers disagree: {got}")
        self.assertIn(rundir.DEFAULT_ACTOR, got[0])

    def test_enrolled_daemon_cli_and_shell_agree(self):
        self.enroll("@enrolled:example")
        got = self.all_three(self.env())
        self.assertEqual(len(set(got)), 1, f"resolvers disagree: {got}")
        self.assertNotIn(rundir.DEFAULT_ACTOR, got[0],
                         "the enrolled actor was ignored")

    def test_named_instance_still_agrees(self):
        self.enroll("@enrolled:example")
        got = self.all_three(self.env(SUTANDO_INSTANCE_ID="research-001"))
        self.assertEqual(len(set(got)), 1, f"resolvers disagree: {got}")

    def test_socket_override_still_wins_everywhere(self):
        env = self.env(SUTANDO_RUNTIME_SOCKET="/tmp/pinned.sock")
        self.assertEqual(set(self.all_three(env)), {"/tmp/pinned.sock"})

    # ── upgrade path: a pre-actor flat socket is present ────────────────────
    def test_upgraded_two_actor_control_never_shares_a_socket(self):
        run = self.base / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "sutando-runtime.sock").touch()  # the pre-actor artifact
        a = self.all_three(self.env(SUTANDO_AGENT_ID="@a:example"))
        b = self.all_three(self.env(SUTANDO_AGENT_ID="@b:example"))
        self.assertEqual(len(set(a)), 1, f"actor a disagrees: {a}")
        self.assertEqual(len(set(b)), 1, f"actor b disagrees: {b}")
        self.assertNotEqual(a[0], b[0], "two actors were handed one socket")

    def test_socket_and_lock_come_from_one_directory(self):
        """The half of the upgrade bug that let one socket answer for two
        locks: whatever the locator returns, the lock lives beside it."""
        for agent in ("@a:example", "@b:example", rundir.DEFAULT_ACTOR):
            with self.subTest(agent=agent):
                sock = rundir.socket_path("default", agent=agent)
                lock = rundir.lock_path("default", agent=agent)
                self.assertEqual(Path(sock).parent, lock.parent)

    # ── negative agreement: they must also FAIL together ────────────────────
    def assert_all_reject(self, env, why):
        rcs = self.all_three_rc(env)
        names = ("daemon", "cli", "shell")
        for name, r in zip(names, rcs):
            self.assertNotEqual(
                r.returncode, 0,
                f"{name} reported success on {why}: {r.stdout.strip()!r}")
            self.assertEqual(r.stdout.strip(), "",
                             f"{name} printed a socket path on {why}")

    def test_over_long_identity_is_rejected_by_all_three(self):
        """A resolver that fails must not be papered over: a synthesized flat
        endpoint is a plausible WRONG answer, which is worse than none."""
        self.assert_all_reject(self.env(SUTANDO_AGENT_ID="@" + "a" * 200),
                               "an over-long agent id")

    def test_unusable_run_dir_is_rejected_by_all_three(self):
        root = self.base
        while len(str(root).encode()) < rundir.SUN_PATH_MAX - 10:
            root = root / "padpadpad"
        root.mkdir(parents=True, exist_ok=True)
        self.assert_all_reject(self.env(SUTANDO_RUN_DIR=str(root)),
                               "a run dir with no AF_UNIX budget")

    def test_positive_control_the_same_probes_succeed_on_valid_config(self):
        """Without this, `assert_all_reject` could be passing because the
        probes are broken rather than because the guard fires."""
        for r in self.all_three_rc(self.env()):
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.strip())

    # ── the live control ───────────────────────────────────────────────────
    def _boot(self, env):
        expected = self.daemon_socket(env)
        proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, cwd=str(ROOT))
        self.addCleanup(self._kill, proc)
        deadline = time.time() + 45
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"daemon exited {proc.returncode}: "
                                     f"{proc.stdout.read()}")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            try:
                s.connect(expected)
                return expected
            except OSError:
                pass
            finally:
                s.close()
            time.sleep(0.2)
        raise AssertionError(f"daemon never listened on {expected}")

    @staticmethod
    def _kill(proc):
        proc.kill()
        proc.wait(timeout=15)

    def test_live_round_trip_with_no_socket_override(self):
        """The shipped default end to end: boot the daemon, then reach it with
        the real CLI, neither told where the socket is."""
        for agent in (None, "@enrolled:example"):
            with self.subTest(enrolled=bool(agent)):
                self.setUp()
                if agent:
                    self.enroll(agent)
                env = self.env()
                self.assertNotIn("SUTANDO_RUNTIME_SOCKET", env)
                sock = self._boot(env)
                out = _run([sys.executable, str(CLI), "request", "list"], env)
                self.assertEqual(out.returncode, 0,
                                 f"CLI could not reach {sock}: {out.stderr}")
                self.assertIn("requests", json.loads(out.stdout))


if __name__ == "__main__":
    unittest.main(verbosity=2)
