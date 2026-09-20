"""Proactive claim recovery probes owners without signalling or stealing from them."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox  # noqa: E402
import proactive_recovery as recovery  # noqa: E402

from ag2_sparrow import outbox as packaged_outbox  # noqa: E402
from ag2_sparrow import proactive_recovery as packaged_recovery  # noqa: E402


class ProactiveProcessProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        ws = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cls.stack.enter_context(patch.dict(os.environ, {
            "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
            "CLAUDE_CONFIG_DIR": str(ws / "config"),
        }))
        from ag2_sparrow import remote_gateway_bridge
        cls.bridge = remote_gateway_bridge
        cls.loader = runpy.run_path(
            str(REPO / "src" / "remote-gateway-bridge.py"), run_name="probe_test_loader")

    def test_gateway_and_loader_use_shared_owner_policy(self):
        self.assertIs(self.bridge._pid_alive, packaged_recovery.claim_owner_may_be_alive)
        self.assertIs(self.loader["_pid_alive"], packaged_recovery.claim_owner_may_be_alive)

    def test_private_and_gateway_claims_only_recover_confirmed_dead_owners(self):
        for module, identity_module in (
            (recovery, outbox), (packaged_recovery, packaged_outbox),
        ):
            for state in identity_module.OwnerState:
                with self.subTest(module=module.__name__, state=state), \
                        tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    private = root / "proactive-private.sending.recover-4242-0"
                    private.write_text("private body", encoding="utf-8")
                    identity = identity_module.ProcessIdentity(4242, state)
                    with patch.object(module, "process_identity", return_value=identity) as probe:
                        count = module.recover_orphan_sending_files(root)
                    dead = state == identity_module.OwnerState.DEAD
                    self.assertEqual(count, int(dead))
                    self.assertEqual(private.exists(), not dead)
                    self.assertEqual((root / "proactive-private.txt").exists(), dead)
                    probe.assert_called_once_with(4242)
                    if module is packaged_recovery:
                        claim = root / "proactive-gateway.sending.4242"
                        claim.write_text("gateway body", encoding="utf-8")
                        with patch.object(self.bridge, "RESULTS_DIR", root), \
                                patch.object(module, "process_identity", return_value=identity) as probe:
                            self.bridge._recover_orphan_proactive()
                        probe.assert_called_once_with(4242)
                        self.assertEqual(claim.exists(), not dead)
                        self.assertEqual((root / "proactive-gateway.txt").exists(), dead)

    def test_real_child_survives_probes_and_is_dead_after_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with patch.object(os, "kill", side_effect=AssertionError("must not signal the owner")):
                for probe in (recovery.claim_owner_may_be_alive, self.bridge._pid_alive,
                              self.loader["_pid_alive"]):
                    self.assertTrue(probe(child.pid))
                    self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=10)
            with patch.object(os, "kill", side_effect=AssertionError("must not signal the owner")):
                for probe in (recovery.claim_owner_may_be_alive, self.bridge._pid_alive,
                              self.loader["_pid_alive"]):
                    self.assertFalse(probe(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
