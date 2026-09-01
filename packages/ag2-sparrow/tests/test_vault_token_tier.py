"""_token_from_vault_ag2space — the vault tier sparrow was missing (#2638 parity).

Before this, sparrow resolved its onboarding token from the process env and the
channel `.env` but NEVER the Keychain vault, so `vault set REMOTE_TASK_TOKEN`
stored the value and changed nothing for ag2space — the operator spent the
secret and saw no effect (the failure #2638 fixed for discord/slack/telegram;
@qingyun-air's 2026-08-04 bridge-parity finding).

Pins the SHIPPED resolver `_token_from_vault_ag2space` (imported from the real
bridge module, not a copy): it resolves REMOTE_TASK_TOKEN, falls back to the
legacy AG2_REMOTE_TOKEN, is total-failure-safe, and degrades to '' (rather than
crash) when the core `channel_token` policy is absent — the standalone-install
case.

**Hermeticity — the load-bearing property (@john-the-dev's review of b53e7bee).**
The bridge resolves its token AT IMPORT: module init runs
`if not _RAW: _RAW = _token_from_vault_ag2space()` with NO injected `vault_get`,
so the shared policy reaches the real `vault_intercept.get_vault_key` during the
import itself — *before* any test body can pass a fake. `CLAUDE_CONFIG_DIR`
isolation does NOT isolate the macOS Keychain, and "vault_get is injectable" is a
property of the *function*, not of the *execution path* (the seam is taken on
every call except the first — module init). So this module shadows
`vault_intercept` in `sys.modules` with a recording fake **before the bridge is
imported at all**; the real `channel_token.token_from_vault` policy stays under
test, only the host Keychain boundary is replaced. `test_module_import_is_hermetic`
is the control proving the import consulted the shadow and never the host vault.

Run: python3 packages/ag2-sparrow/tests/test_vault_token_tier.py
"""
import os
import sys
import types
import tempfile
import importlib
import pathlib
import unittest


# --------------------------------------------------------------------------- #
# Shadow the vault boundary BEFORE the bridge is ever imported. Installed at
# MODULE level (this runs on import of the test file, ahead of the first _load),
# so no code path — import-time resolution included — can reach the host Keychain.
# --------------------------------------------------------------------------- #
_VAULT_STORE: dict[str, str] = {}   # var -> value; a missing var raises KeyError
_VAULT_CALLS: list[str] = []        # every var the shadow was asked for


def _fake_get_vault_key(var):
    _VAULT_CALLS.append(var)
    if var in _VAULT_STORE:
        return _VAULT_STORE[var]
    raise KeyError(var)             # mirror the real get_vault_key "absent" contract


_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = _fake_get_vault_key
sys.modules["vault_intercept"] = _FAKE_VI   # replace the host boundary pre-import


def _load(tmp):
    # Hermetic: no host config is read at import (bridge reads config during
    # exec_module — see lint-hermetic-bridge-tests). Clear every token source so
    # the module-level resolution reaches the vault tier under test, and reset the
    # shadow so each import starts from a known (empty) vault.
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    for k in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_URL",
              "AG2_REMOTE_URL", "AG2_DEVICE_ENV", "GATEWAY_INSTANCE"):
        os.environ.pop(k, None)
    _VAULT_CALLS.clear()
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


class VaultTierTests(unittest.TestCase):
    def setUp(self):
        _VAULT_STORE.clear()
        _VAULT_CALLS.clear()

    # ---- the resolver itself (injected vault_get — pins the shipped policy) ---- #
    def test_resolves_current_name(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            vault = {"REMOTE_TASK_TOKEN": "https://gw.example/relay|s3cr3t"}
            self.assertEqual(
                m._token_from_vault_ag2space(vault_get=lambda k: vault.get(k)),
                "https://gw.example/relay|s3cr3t")

    def test_falls_back_to_legacy_alias(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            vault = {"AG2_REMOTE_TOKEN": "legacy-secret"}
            self.assertEqual(
                m._token_from_vault_ag2space(vault_get=lambda k: vault.get(k)), "legacy-secret")

    def test_total_failure_safe(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            self.assertEqual(m._token_from_vault_ag2space(vault_get=lambda k: None), "")

            def boom(k):
                raise RuntimeError("keychain locked")

            self.assertEqual(m._token_from_vault_ag2space(vault_get=boom), "")

    def test_degrades_when_core_absent(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            real_isfile = os.path.isfile
            os.path.isfile = lambda p: False   # channel_token.py "not found"
            try:
                got = m._token_from_vault_ag2space(vault_get=lambda k: "would-be-token")
            finally:
                os.path.isfile = real_isfile
            self.assertEqual(got, "")

    def test_safe_on_broken_core_import(self):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            broken = types.ModuleType("channel_token")   # lacks token_from_vault
            saved = sys.modules.get("channel_token")
            sys.modules["channel_token"] = broken
            try:
                got = m._token_from_vault_ag2space(vault_get=lambda k: "would-be-token")
            finally:
                if saved is not None:
                    sys.modules["channel_token"] = saved
                else:
                    sys.modules.pop("channel_token", None)
            self.assertEqual(got, "")

    # ---- hermeticity control (@john-the-dev) ---- #
    def test_module_import_is_hermetic(self):
        # Empty shadow: module init resolves the token at import and must consult
        # ONLY the shadow, never the host Keychain, and derive no token from it.
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
        # The real vault_intercept was replaced in sys.modules before the bridge
        # was imported, so the host Keychain is structurally unreachable...
        self.assertIs(sys.modules["vault_intercept"], _FAKE_VI)
        # ...and the shadow WAS the boundary the import went through (proof the
        # import-time resolution is fully controlled by it, not by any un-shadowed
        # path), asking for the real producer's var and getting nothing.
        self.assertIn("REMOTE_TASK_TOKEN", _VAULT_CALLS)
        self.assertEqual(m.TOKEN, "")

    def test_import_time_resolution_flows_through_the_shadow(self):
        # Positive control: a token IN the shadow is what module init resolves —
        # so shadowing the boundary fully determines import-time auth state, i.e.
        # nothing reaches past the shadow to the host vault.
        _VAULT_STORE["REMOTE_TASK_TOKEN"] = "https://gw.example/relay|from-vault"
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
        self.assertEqual(m.TOKEN, "from-vault")
        self.assertEqual(m.URL, "https://gw.example/relay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
