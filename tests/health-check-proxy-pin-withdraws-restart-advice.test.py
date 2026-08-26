#!/usr/bin/env python3
"""A pin on the credential proxy must WITHDRAW the restart prescription.

The prior fix added `restart_veto` and stopped there. Both quota consumers
return ok/warn, are benign under `is_issue()`, and never reach the `--fix`
loop -- so the field blocked nothing while "Then restart the proxy" stayed on
screen. Worst case: the owner follows it and destroys the pinned process.

These rows drive the PRODUCTION path. Deleting the producer in
`check_credential_proxy`, or the `restart_veto=` threading at the call site,
must fail them -- the previous suite stayed green through exactly that deletion.
"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("hc_pin", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)

PIN = "pinned by lldb (pid 4242)"


class ProxyPinWithdrawsRestartAdvice(unittest.TestCase):

    def _healthy_proxy(self, armed):
        """A HEALTHY (ok, not stale) proxy — the arm the old code never reached."""
        with patch.object(hc, "check_port", return_value={"name": "credential-proxy", "status": "ok"}), \
             patch.object(hc, "mark_stale_if_outdated", lambda *a, **k: None), \
             patch.object(hc, "_process_executes_artifact", return_value=False), \
             patch.object(hc, "_proc_lstarts", return_value=([], {})), \
             patch.object(hc.process_pins, "armed_detail", return_value=armed):
            return hc.check_credential_proxy()

    def test_producer_sets_the_veto_on_a_HEALTHY_pinned_proxy(self):
        # Deleting the producer block makes this fail; the old suite could not.
        self.assertEqual(self._healthy_proxy(PIN).get("restart_veto"), PIN)

    def test_unpinned_healthy_proxy_invents_no_veto(self):
        self.assertIsNone(self._healthy_proxy(None).get("restart_veto"))

    def _identity(self, veto):
        """Drive the PUBLIC entry point, not the verdict helper. Testing
        `_quota_identity_verdict` directly leaves the `restart_veto=` threading
        in `check_quota_account_identity` uncovered -- verified by mutation:
        removing it left a helper-level suite fully green."""
        def _svc(cfg):
            return "Claude Code-credentials" if cfg == "/core/cfg" else "Claude Code-credentials-other"
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/core/cfg"}), \
             patch.object(hc, "_resolved_credential_service", _svc), \
             patch.object(hc, "_proxy_config_dir_from_process", return_value="/other/cfg"):
            return hc.check_quota_account_identity(
                "ok", core_env_prober=lambda *a, **k: True, restart_veto=veto)

    def test_pinned_row_says_DO_NOT_RESTART_and_drops_the_restart_advice(self):
        d = self._identity(PIN)["detail"]
        self.assertIn("DO NOT RESTART", d)
        self.assertIn(PIN, d)
        self.assertNotIn("Then restart the proxy", d)

    def test_pinned_row_KEEPS_the_diagnosis(self):
        """Withdrawing the remedy must not withdraw the finding.

        Assert the branch-INDEPENDENT part. The remedy sentence names
        CLAUDE_CONFIG_DIR only when a plist exists, so asserting that string
        passes on a host with one and fails in CI, which has none.
        """
        d = self._identity(PIN)["detail"]
        self.assertIn("DIFFERENT login", d)
        self.assertIn("Claude Code-credentials-other", d)

    def test_unpinned_row_retains_the_normal_remedy(self):
        d = self._identity(None)["detail"]
        self.assertIn("Then restart the proxy", d)
        self.assertNotIn("DO NOT RESTART", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
