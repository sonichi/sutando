#!/usr/bin/env python3
"""Tests for instance-scoped runtime resources (isolation spec V1).

Contract: each instance gets its own run dir, socket and lock so two
instances can never collide; the default instance keeps honoring a
pre-existing legacy flat socket; SUTANDO_RUNTIME_SOCKET still overrides.

Identity here is the SAME (agent_id, instance_id) tuple the registry keys on:
two actors both using instance_id "default" are two instances and must be
able to run at once, which scoping by instance alone made impossible.

Run: python3 tests/runtime-api-rundir-instances.test.py
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import rundir  # noqa: E402

# SUTANDO_RUNTIME_STATE is part of the identity env: the actor chain reads the
# enrolled record there, so leaving it unset would read the real workspace.
_IDENTITY_ENV = ("SUTANDO_RUNTIME_SOCKET", "SUTANDO_INSTANCE_ID",
                 "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID",
                 "SUTANDO_RUNTIME_STATE")


class RundirInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.pop(k, None) for k in _IDENTITY_ENV}
        os.environ["SUTANDO_RUN_DIR"] = self.tmp.name
        os.environ["SUTANDO_RUNTIME_STATE"] = str(Path(self.tmp.name) / "state")

    def tearDown(self):
        os.environ.pop("SUTANDO_RUN_DIR", None)
        for k in _IDENTITY_ENV:
            os.environ.pop(k, None)
            if self.saved.get(k) is not None:
                os.environ[k] = self.saved[k]
        self.tmp.cleanup()

    def test_instances_get_disjoint_sockets_and_locks(self):
        a = rundir.socket_path("qingyun-001")
        b = rundir.socket_path("research-001")
        self.assertNotEqual(a, b)
        self.assertIn("qingyun-001", Path(a).parent.name)
        self.assertNotEqual(rundir.lock_path("qingyun-001"),
                            rundir.lock_path("research-001"))

    def test_env_instance_id_scopes_the_default(self):
        os.environ["SUTANDO_INSTANCE_ID"] = "research-001"
        self.assertIn("research-001",
                      Path(rundir.socket_path()).parent.name)

    def test_default_honors_preexisting_legacy_flat_socket(self):
        legacy = Path(self.tmp.name) / "sutando-runtime.sock"
        self.assertNotEqual(rundir.socket_path(), str(legacy))  # absent -> scoped
        legacy.touch()
        self.assertEqual(rundir.socket_path(), str(legacy))     # present -> honored
        # non-default instances NEVER fall back to the shared legacy socket
        self.assertNotEqual(rundir.socket_path("research-001"), str(legacy))

    def test_explicit_env_socket_still_wins(self):
        os.environ["SUTANDO_RUNTIME_SOCKET"] = "/x/y.sock"
        self.assertEqual(rundir.socket_path("anything"), "/x/y.sock")

    def test_two_actors_sharing_an_instance_id_get_disjoint_resources(self):
        a_sock = rundir.socket_path("default", agent="a")
        b_sock = rundir.socket_path("default", agent="b")
        self.assertNotEqual(a_sock, b_sock, "two actors shared one socket")
        self.assertNotEqual(rundir.lock_path("default", agent="a"),
                            rundir.lock_path("default", agent="b"),
                            "two actors shared one instance lock")

    def test_actor_identity_comes_from_the_same_env_chain_as_the_daemon(self):
        os.environ["SUTANDO_AGENT_ID"] = "a"
        from_env = rundir.socket_path("default")
        os.environ["SUTANDO_AGENT_ID"] = "b"
        self.assertNotEqual(from_env, rundir.socket_path("default"))

    def test_actor_segment_is_escaped_not_sanitized(self):
        self.assertNotEqual(rundir.instance_run_dir("default", agent="blue/red"),
                            rundir.instance_run_dir("default", agent="blue_red"))

    def test_no_actor_declared_still_resolves_the_shared_default_actor(self):
        """No declared actor is not "no actor": daemon, CLI and shell must all
        land on DEFAULT_ACTOR, or a fresh daemon is unreachable from its own
        CLI (review P1 regression)."""
        self.assertEqual(
            rundir.instance_run_dir("solo"),
            Path(self.tmp.name) / rundir.instance_key(rundir.DEFAULT_ACTOR, "solo"))


class LiveDoubleStartTests(unittest.TestCase):
    """The live control: two actors that the registry lists as distinct must
    actually be able to run at the same time under one run dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait(timeout=10)
        self.tmp.cleanup()

    def _start(self, agent, tag=None):
        root = Path(self.tmp.name) / (tag or agent)
        env = {**os.environ,
               "SUTANDO_RUN_DIR": str(Path(self.tmp.name) / "run"),
               "SUTANDO_AGENT_ID": agent,
               "SUTANDO_INSTANCE_ID": "default",
               "SUTANDO_RUNTIME_DB": str(root / "runtime.sqlite"),
               "SUTANDO_HA_DIR": str(root / "human-actions"),
               "SUTANDO_RUNTIME_STATE": str(root / "state"),
               "SUTANDO_INSTANCE_REGISTRY": str(root / "instances")}
        for k in ("SUTANDO_RUNTIME_SOCKET", "AGENT_MXID", "AGENT_ID"):
            env.pop(k, None)
        p = subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "runtime-api" / "server.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p, root

    @staticmethod
    def _reachable(path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect(path)
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _daemon_outcome(self, proc, root, timeout=30.0):
        """None once the daemon is live (registered + its endpoint answers),
        else its exit code. Readiness is positive, so a slow boot cannot read
        as success."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return proc.returncode
            for m in sorted((root / "instances").glob("*.json")):
                try:
                    ep = (json.loads(m.read_text()).get("endpoint") or {}).get("path")
                except (OSError, ValueError):
                    continue
                if ep and self._reachable(ep):
                    return None
            time.sleep(0.2)
        raise AssertionError(f"daemon neither became live nor exited in {timeout}s")

    def test_second_actor_on_the_default_instance_still_starts(self):
        a, a_root = self._start("a")
        self.assertIsNone(self._daemon_outcome(a, a_root), "first daemon never came up")
        b, b_root = self._start("b")
        rc = self._daemon_outcome(b, b_root)
        self.assertIsNone(rc, "a second ACTOR on instance 'default' was refused "
                              f"(rc={rc}): {b.stdout.read() if rc is not None else ''}")

    def test_same_actor_and_instance_is_still_a_refused_double_start(self):
        a, a_root = self._start("a")
        self.assertIsNone(self._daemon_outcome(a, a_root))
        dup, dup_root = self._start("a", tag="a-dup")
        self.assertEqual(self._daemon_outcome(dup, dup_root), 1,
                         "a genuine double start of ONE instance was allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
