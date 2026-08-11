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

    def _routed_env(self, core_cfg, routed=True):
        """Env context that is HERMETIC about the routing gate.

        `mock.patch.dict(..., clear=False)` inherits the ambient environment, so
        on a developer host whose core IS proxy-routed these cases silently
        inherited a real ANTHROPIC_BASE_URL and passed for the wrong reason —
        green locally, red on CI, which is exactly what happened on 211b97a1.
        Every case must state its own routing premise rather than borrow the
        host's.
        """
        env = {"CLAUDE_CONFIG_DIR": core_cfg} if core_cfg else {}
        if routed:
            env["ANTHROPIC_BASE_URL"] = "http://localhost:7846"
        ctx = mock.patch.dict(os.environ, env, clear=False)
        if routed:
            return ctx
        # Not-routed must be asserted, not assumed: strip any inherited value.
        outer = mock.patch.dict(os.environ, env, clear=False)

        class _Unrouted:
            def __enter__(self_inner):
                outer.__enter__()
                os.environ.pop("ANTHROPIC_BASE_URL", None)
                return self_inner

            def __exit__(self_inner, *exc):
                return outer.__exit__(*exc)

        return _Unrouted()

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

    def _run(self, core_cfg, plist_cfg, existing_services, proxy_status="ok",
             routed=True, codex_runtime=False):
        """`routed` controls the ANTHROPIC_BASE_URL the core would have inherited;
        every case must state it, because the check is gated on it."""
        self._write_plist(plist_cfg)
        with self._routed_env(core_cfg, routed=routed), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=codex_runtime), \
             mock.patch.object(hc, "_keychain_service_exists",
                               side_effect=lambda s: s in existing_services):
            if not core_cfg:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            # The PROBE is what the gate consults — never this process's env.
            return hc.check_quota_account_identity(
                proxy_status, core_env_prober=lambda: routed)

    # ---- routing gate: the false-positive class qingyun-wu blocked on --------

    def test_proxy_up_but_core_not_routed_does_not_warn(self):
        """THE routing-gate pin. Divergent keychain items AND a live proxy, but
        this core has no ANTHROPIC_BASE_URL — it does not send requests through
        that proxy at all. Every clause of the warning ("requests bill that
        account", "/login here will not reach the proxy") would be false, so the
        check must stay silent rather than describe a relationship that does not
        exist."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA}, routed=False)
        self.assertEqual(out["status"], "ok",
                         "an unrouted core must not be told the proxy is billing its requests")
        self.assertIn("not routed", out["detail"])

    def test_probes_the_CORE_env_not_this_process(self):
        """THE qingyun-wu P1 pin: health-check's OWN environment lacks
        ANTHROPIC_BASE_URL while the probed running core HAS it.

        The first gate read `os.environ`, on the theory that health-check is a
        child of the core and inherits it. That holds on the proactive-loop path
        and fails on the app / fallback-launchd / manual paths, which this file
        documents do not carry the core's env. There, a routed core with a real
        account mismatch would read "comparison inactive" and go silent — the
        check disabled precisely where no human is watching.

        So: subprocess env stripped, prober says True, and the comparison must
        still run to a WARN on divergent items."""
        core = "/Users/x/ws/.claude-sutando"
        self._write_plist(None)                      # proxy resolves vanilla
        env = {"CLAUDE_CONFIG_DIR": core}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False), \
             mock.patch.object(hc, "_keychain_service_exists",
                               side_effect=lambda s: s in {_scoped(core), VANILLA}):
            os.environ.pop("ANTHROPIC_BASE_URL", None)   # THIS process is unrouted
            out = hc.check_quota_account_identity("ok", core_env_prober=lambda: True)
        self.assertEqual(out["status"], "warn",
                         "a routed CORE must be compared even when health-check's own "
                         "env lacks the variable")
        self.assertIn(_scoped(core), out["detail"])

    def test_undeterminable_core_env_is_silent_and_says_so(self):
        """`core_env_has_proxy_url` is tri-state and None must NEVER collapse to
        False. Undeterminable (no tmux, ambiguous session) is not evidence of a
        bypass — stay silent, and state the no-op rather than returning a bare
        ok indistinguishable from a real comparison."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA}, routed=None)
        self.assertEqual(out["status"], "ok")
        self.assertIn("inactive", out["detail"])

    def test_non_proxy_runtime_does_not_warn(self):
        """Same divergence, but the runtime marker says this core is not
        proxy-routed (Codex). check_quota_telemetry gates on the same marker."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA}, codex_runtime=True)
        self.assertEqual(out["status"], "ok")
        self.assertIn("not proxy-routed", out["detail"])

    # ---- parseable-but-wrong-shape plists: must warn, never raise -----------

    def test_env_block_wrong_type_warns_not_raises(self):
        """A plist that PARSES with `EnvironmentVariables` as a string. `.get` on
        a str raises AttributeError, which the OSError/ValueError handler does not
        catch — it would abort the entire health run and take every later check
        with it. Reproduced by qingyun-wu on d208c539."""
        core = "/Users/x/ws/.claude-sutando"
        path = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
        path.write_bytes(plistlib.dumps({"EnvironmentVariables": "not-a-dict"}))
        with self._routed_env(core), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False):
            out = hc.check_quota_account_identity(
                "ok", core_env_prober=lambda: True)   # must not raise
        self.assertEqual(out["status"], "warn")
        self.assertIn("EnvironmentVariables", out["detail"])

    def test_plist_root_wrong_type_warns_not_raises(self):
        """Widening the same axis rather than patching the one case found: a
        plist whose ROOT is an array parses fine and has no `.get` either."""
        core = "/Users/x/ws/.claude-sutando"
        path = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
        path.write_bytes(plistlib.dumps(["not", "a", "dict"]))
        with self._routed_env(core), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False):
            out = hc.check_quota_account_identity("ok", core_env_prober=lambda: True)
        self.assertEqual(out["status"], "warn")
        self.assertIn("root", out["detail"])

    def test_config_dir_wrong_type_warns_not_raises(self):
        """Third point on the axis: the key exists but holds an integer, so it
        cannot be hashed into a keychain service name."""
        core = "/Users/x/ws/.claude-sutando"
        path = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
        path.write_bytes(plistlib.dumps({"EnvironmentVariables": {"CLAUDE_CONFIG_DIR": 42}}))
        with self._routed_env(core), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False):
            out = hc.check_quota_account_identity("ok", core_env_prober=lambda: True)
        self.assertEqual(out["status"], "warn")
        self.assertIn("CLAUDE_CONFIG_DIR", out["detail"])

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

    def test_stale_proxy_is_still_compared(self):
        """A "stale" proxy is LISTENING — it is injecting credentials, using
        pre-deploy code. Lumping it in with down/warn would silence the
        comparison exactly during a redeploy, when the two sides are most
        likely to have drifted."""
        core = "/Users/x/ws/.claude-sutando"
        out = self._run(core_cfg=core, plist_cfg=None,
                        existing_services={_scoped(core), VANILLA},
                        proxy_status="stale")
        self.assertEqual(out["status"], "warn",
                         "a stale proxy still injects — the comparison must run")
        self.assertIn(_scoped(core), out["detail"])

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

    # ---- degraded environments: never crash the whole health run ----------

    def test_missing_plist_is_ok_not_an_error(self):
        """Proxy running but not launchd-managed (dev host, manual launch).
        There is no plist to compare against; that is not a fault."""
        core = "/Users/x/ws/.claude-sutando"
        with self._routed_env(core), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False):
            out = hc.check_quota_account_identity(
                "ok", core_env_prober=lambda: True)   # no plist written
        self.assertEqual(out["status"], "ok")
        self.assertIn("launchd", out["detail"])

    def test_corrupt_plist_warns_rather_than_raising(self):
        """A truncated/garbage plist must degrade to a warn, not propagate an
        exception into the health run and take every later check with it."""
        core = "/Users/x/ws/.claude-sutando"
        path = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
        path.write_bytes(b"\x00not a plist\x00")
        with self._routed_env(core), \
             mock.patch.object(hc.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(hc, "_runtime_may_skip_proxy", return_value=False):
            out = hc.check_quota_account_identity("ok", core_env_prober=lambda: True)
        self.assertEqual(out["status"], "warn")
        self.assertIn("cannot read", out["detail"])

    def test_keychain_probe_failure_is_swallowed(self):
        """`security` missing or the keychain locked must read as 'no such
        item', not raise. Non-macOS CI hits this path."""
        with mock.patch.object(hc.subprocess, "run", side_effect=OSError("no security binary")):
            self.assertFalse(hc._keychain_service_exists(VANILLA))
        with mock.patch.object(hc.subprocess, "run",
                               side_effect=hc.subprocess.SubprocessError("timeout")):
            self.assertFalse(hc._keychain_service_exists(VANILLA))

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
