#!/usr/bin/env python3
"""One bridge per agent identity on a host (ag2_sparrow.identity_lock and the bridge's use of it).

On 2026-11-25 an agent lost about half of its tasks: a stale connector from another install polled the
relay as the same agent, from another folder, so the workspace lock never saw it. The second bridge for
an identity must not poll, and must say why.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp()
os.environ["AGENT_CONNECT_STATE_DIR"] = str(Path(_TMP) / "state")
os.environ["AGENT_CONNECT_TASK_DIR"] = str(Path(_TMP) / "tasks")
os.environ["AGENT_CONNECT_RESULT_DIR"] = str(Path(_TMP) / "results")
os.environ["SUTANDO_HOST_LABEL"] = "testhost"
os.environ.setdefault("REMOTE_TASK_TOKEN", "https://example|secret")

sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402
from ag2_sparrow import gateway_instance, identity_lock  # noqa: E402

AGENT, URL = "@probe.agent:s.space", "https://gateway.example/relay"

# Another bridge for the same agent: its own folder, the same per-install key. It holds the lock until told to go.
OTHER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ag2_sparrow import gateway_instance, identity_lock
gateway_instance.binding_key_path = lambda: Path(sys.argv[2]) / "binding.key"
print(identity_lock.take(sys.argv[3], sys.argv[4], declares={"serving": "/other/install"})[0], flush=True)
sys.stdin.readline()
"""


class IdentityLockTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        key = mock.patch.object(gateway_instance, "binding_key_path", lambda: self.home / "binding.key")
        key.start()
        self.addCleanup(key.stop)
        gateway_instance.binding_digest(AGENT, URL, create=True)     # the install's key, as owner routing makes it
        for name, value in (("_reenroll_identity", lambda: AGENT), ("URL", URL)):
            patch = mock.patch.object(rgb, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        rgb._IDENTITY_LOCK["name"] = ""
        self.logs = []
        log = mock.patch.object(rgb, "_log", self.logs.append)
        log.start()
        self.addCleanup(log.stop)
        self.status = []
        emit = mock.patch.object(rgb, "_emit_gateway_status", lambda ok, **kw: self.status.append((ok, kw)))
        emit.start()
        self.addCleanup(emit.stop)

    def other_bridge(self, agent=AGENT):
        child = subprocess.Popen([sys.executable, "-c", OTHER, str(REPO / "packages" / "ag2-sparrow"), str(self.home),
                                  agent, URL], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                 env=isolated_child_env())
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                 child.stdin.close()))
        self.assertEqual(child.stdout.readline().strip(), identity_lock.HELD)
        return child

    def test_a_second_bridge_for_the_same_agent_from_another_folder_does_not_poll_and_says_why(self):
        other = self.other_bridge()
        self.assertFalse(rgb._acquire_identity(), "the identity is being served: this bridge must not poll")
        said = " ".join(self.logs)
        self.assertIn("already receives this agent's tasks", said)
        self.assertIn("pid %d" % other.pid, said)
        self.assertIn("/other/install", said, "the owner is told which install holds it")
        (ok, kw), = self.status
        self.assertEqual((ok, "already receives this agent's tasks" in kw["error"]), (False, True))
        self.assertEqual(rgb._IDENTITY_LOCK["name"], "")
        # The other bridge is gone: its lock is a dead pid's, and this one takes over.
        other.stdin.close()
        other.wait(timeout=10)
        self.assertTrue(rgb._acquire_identity())
        self.assertTrue(rgb._IDENTITY_LOCK["name"].startswith(identity_lock.ROLE_PREFIX))
        self.addCleanup(identity_lock.release, rgb._IDENTITY_LOCK["name"])

    def test_bridges_for_two_agents_do_not_shut_each_other_out(self):
        self.other_bridge("@someone-else.agent:s.space")
        self.assertTrue(rgb._acquire_identity())
        self.addCleanup(identity_lock.release, rgb._IDENTITY_LOCK["name"])

    def test_the_lock_is_kept_fresh_released_on_exit_and_losing_it_stops_the_poll(self):
        self.assertTrue(rgb._acquire_identity())
        name = rgb._IDENTITY_LOCK["name"]
        lock = identity_lock.place() / "state" / "locks" / (name + ".lock")
        first = json.loads(lock.read_text())
        self.assertEqual((first["pid"], first["serving"]), (os.getpid(), str(rgb._LOCK_WS)))
        self.assertNotIn(AGENT.strip("@").split(":")[0], name, "the lock's name says nothing of the agent")
        time.sleep(1.1)
        self.assertTrue(rgb._heartbeat_identity())
        self.assertGreater(json.loads(lock.read_text())["heartbeat_at"], first["heartbeat_at"])
        with mock.patch.object(identity_lock, "beat", lambda name: False):   # reaped by another bridge
            self.assertFalse(rgb._heartbeat_identity())
        self.assertIn("lost to another bridge", " ".join(self.logs))
        identity_lock.release(name)
        self.assertFalse(lock.exists())

    def test_the_bridges_own_lock_heartbeat_and_exit_carry_the_identity_lock(self):
        self.assertTrue(rgb._acquire_singleton())
        self.assertTrue(rgb._acquire_identity())
        name = rgb._IDENTITY_LOCK["name"]
        lock = identity_lock.place() / "state" / "locks" / (name + ".lock")
        self.assertTrue(rgb._heartbeat_singleton())
        with mock.patch.object(identity_lock, "beat", lambda name: False):
            self.assertFalse(rgb._heartbeat_singleton(), "the identity went to another bridge: stop polling")
        self.assertTrue(rgb._LOCK_LOST.is_set())
        rgb._LOCK_LOST.clear()
        with mock.patch.object(identity_lock, "release", side_effect=OSError("disk")):
            rgb._release_singleton()             # a release that fails never stops the exit
        self.assertTrue(lock.exists())
        self.assertTrue(rgb._acquire_singleton())
        rgb._release_singleton()
        self.assertFalse(lock.exists(), "the exit lets the next bridge for this agent in at once")

    def test_an_agent_not_known_yet_takes_the_lock_at_the_first_heartbeat_that_knows_it(self):
        with mock.patch.object(rgb, "_reenroll_identity", lambda: ""):
            self.assertTrue(rgb._acquire_identity(), "no identity yet: fail-open")
            self.assertEqual(rgb._IDENTITY_LOCK["name"], "")
        self.other_bridge()
        self.assertFalse(rgb._heartbeat_identity(), "known now, and already served: stop polling")

    def test_no_key_a_lock_error_or_the_kill_switch_never_stop_the_bridge(self):
        with mock.patch.object(gateway_instance, "binding_key_path", lambda: self.home / "none" / "binding.key"):
            self.assertEqual(identity_lock.take(AGENT, URL)[0], identity_lock.UNKNOWN)
            self.assertTrue(rgb._acquire_identity())
        with mock.patch.object(identity_lock, "take", side_effect=RuntimeError("boom")):
            self.assertTrue(rgb._acquire_identity())
        self.assertIn("proceeding without it", " ".join(self.logs))
        with mock.patch.object(identity_lock, "beat", side_effect=OSError("disk")):
            rgb._IDENTITY_LOCK["name"] = "bridge-identity.x"
            self.assertTrue(rgb._heartbeat_identity())
            rgb._IDENTITY_LOCK["name"] = ""
        self.other_bridge()
        with mock.patch.dict(os.environ, {"SUTANDO_BRIDGE_LOCK": "0"}):
            self.assertTrue(rgb._acquire_identity())
        with mock.patch.object(gateway_instance, "binding_key_path", side_effect=OSError("no home")):
            self.assertEqual(identity_lock.take(AGENT, URL)[0], identity_lock.UNKNOWN)
        with mock.patch.object(identity_lock.workspace_lock, "acquire", side_effect=OSError("read-only")):
            self.assertEqual(identity_lock.take(AGENT, URL)[0], identity_lock.UNKNOWN)
        self.assertTrue(identity_lock.beat(""))
        identity_lock.release("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
