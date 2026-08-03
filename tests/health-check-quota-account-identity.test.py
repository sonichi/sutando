#!/usr/bin/env python3
"""check_quota_account_identity — does the proxy inject THIS core's login?

Pins the failure observed 2026-08-03: the credential proxy was up, routing, and
writing a seconds-old quota-state.json **for a different account**. The owner's
login showed 7% of the 7d window used while every routed request billed an
account at 88%, and the core throttled itself for an hour against a ceiling that
was not his. `check_quota_telemetry` never fired, because every one of its
branches asks WHEN (stale? never written?) and none asks WHOSE.

The load-bearing case here is `test_divergent_config_dirs_warn`: it FAILS on the
parent commit, where no such check exists. The agreeing cases would pass against
any implementation, including one that always returns ok, so on their own they
prove nothing.

Keychain access is stubbed — these tests never touch the real keychain and never
read a token. The production code compares keychain ITEM NAMES only.
"""
import hashlib
import importlib.util
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
VANILLA = "Claude Code-credentials"


def _load_health_check():
    spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["health_check"] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_health_check()


def _scoped(config_dir: str) -> str:
    return f"{VANILLA}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


class TestQuotaAccountIdentity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _write_plist(self, config_dir):
        """Render a credential-proxy plist. config_dir=None omits the key —
        the pre-fix shape, which is the whole point of the divergence case."""
        env = {"HOME": str(self.home), "PATH": "/usr/bin"}
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        path = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
        path.write_bytes(plistlib.dumps({"Label": "com.sutando.credential-proxy",
                                         "EnvironmentVariables": env}))
        return path

    def _run(self, core_cfg, plist_cfg, existing_services, proxy_status="ok"):
        self._write_plist(plist_cfg)
        env = {"CLAUDE_CONFIG_DIR": core_cfg} if core_cfg else {}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_keychain_service_exists",
                               side_effect=lambda s: s in existing_services):
            if not core_cfg:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            return hc.check_quota_account_identity(proxy_status)

    # ---- THE regression pin: fails on the parent commit -------------------

    def test_divergent_config_dirs_warn(self):
        """Core is namespaced and its scoped item exists; the plist omits
        CLAUDE_CONFIG_DIR so the proxy can only reach the vanilla item. Both
        exist, so the two sides resolve DIFFERENT logins — exactly the live
        2026-08-03 failure. Must warn."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA})
        self.assertEqual(out["status"], "warn", "divergent logins must not read ok")
        self.assertIn(_scoped(core), out["detail"], "must name the core's item")
        self.assertIn(VANILLA, out["detail"], "must name the proxy's item")
        self.assertIn("CLAUDE_CONFIG_DIR", out["detail"], "must name the cause")

    def test_warn_names_a_concrete_remedy(self):
        """A warning an operator cannot act on is a warning they will learn to
        ignore — the detail must name the plist and the reload."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA})
        self.assertIn("com.sutando.credential-proxy.plist", out["detail"])
        self.assertIn("reload", out["detail"].lower())

    # ---- agreement cases: must NOT warn -----------------------------------

    def test_matching_config_dirs_ok(self):
        """Plist pins the same config dir the core uses — the post-fix state."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=core,
                        existing_services={_scoped(core), VANILLA})
        self.assertEqual(out["status"], "ok")
        self.assertIn(_scoped(core), out["detail"])

    def test_no_scoped_item_means_both_fall_back_to_vanilla(self):
        """Core is namespaced but has never logged in there, so its scoped item
        does not exist and it falls back to vanilla — same as the proxy. Not a
        divergence, and must not warn: this is the ordinary single-login host."""
        out = self._run(core_cfg="/Users/x/ws/.claude-sutando", plist_cfg=None,
                        existing_services={VANILLA})
        self.assertEqual(out["status"], "ok")

    def test_core_without_config_dir_is_ok(self):
        """Vanilla core: both sides resolve the vanilla item by definition."""
        out = self._run(core_cfg=None, plist_cfg=None, existing_services={VANILLA})
        self.assertEqual(out["status"], "ok")

    def test_proxy_down_is_not_a_divergence(self):
        """Nothing is being injected, so there is nothing to disagree about.
        Reporting a mismatch here would be noise on every proxy-less host."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA}, proxy_status="warn")
        self.assertEqual(out["status"], "ok")

    def test_no_readable_credential_states_the_no_op(self):
        """Locked keychain / fresh host: an unqualified ok would be
        indistinguishable from a check that actually compared something."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None, existing_services=set())
        self.assertEqual(out["status"], "ok")
        self.assertIn("inactive", out["detail"])

    # ---- the mirror of the proxy's own contract ---------------------------

    def test_scoped_service_matches_the_proxy_algorithm(self):
        """`_scoped_keychain_service` must mirror credential-proxy.ts exactly:
        sha256(dir)[0:8], and empty/whitespace -> None (vanilla fallback). If
        these drift, the check silently compares the wrong names."""
        d = "/Users/x/ws/.claude-sutando"
        self.assertEqual(hc._scoped_keychain_service(d), _scoped(d))
        self.assertIsNone(hc._scoped_keychain_service(""))
        self.assertIsNone(hc._scoped_keychain_service("   "))
        self.assertIsNone(hc._scoped_keychain_service(None))

    def test_reads_no_secret_material(self):
        """The check must never invoke `security ... -w` (the flag that prints
        the password). Item-existence only."""
        core = "/Users/x/ws/.claude-sutando"
        with mock.patch.object(hc.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            hc._resolved_credential_service(core)
        for call in run.call_args_list:
            self.assertNotIn("-w", call.args[0], "must not read the secret value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
