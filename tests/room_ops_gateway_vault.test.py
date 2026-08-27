#!/usr/bin/env python3
"""gateway() vault tier (sonichi#2638 parity) — coverage-gate home.

The vault fallback in `skills/agent-room-ops/_gateway.py` is exercised by the
room-ops suite, but the diff-coverage gate discovers only `tests/*.test.py`
(`scripts/coverage-gate.sh` → `find tests -name '*.test.py'`), and the room-ops
suite lives under `skills/agent-room-ops/`. Without a test HERE the changed
`_gateway.py` lines are run by the functional job but invisible to the coverage
job — a passing test the gate never sees (same reachability trap as an
unregistered file). This file drives the SHIPPED `_gateway` symbols in-process so
the gate measures them.

Hermeticity: gateway() resolves the token from the Keychain when the env has no
token, and module init does not — but a test that calls gateway() with an empty
env would reach the real `vault_intercept.get_vault_key`. So this shadows
`vault_intercept` in `sys.modules` with a recording fake BEFORE `_gateway` is
imported; the real `channel_token.token_from_vault` policy stays under test.

Run: python3 tests/room_ops_gateway_vault.test.py
"""
import os
import sys
import types
import pathlib
import unittest
from unittest import mock

# Shadow the vault boundary before _gateway (and, lazily, channel_token) can reach
# it — installed at module level, ahead of the import below.
_VAULT_STORE: dict = {}
_VAULT_CALLS: list = []


def _fake_get_vault_key(var):
    _VAULT_CALLS.append(var)
    if var in _VAULT_STORE:
        return _VAULT_STORE[var]
    raise KeyError(var)             # mirror the real "absent key" contract


_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = _fake_get_vault_key
sys.modules["vault_intercept"] = _FAKE_VI

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import _gateway  # noqa: E402

_ENVK = ("GATEWAY_URL", "GATEWAY_TOKEN", "RELAY_URL", "REMOTE_TASK_URL",
         "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")


class GatewayVaultTier(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENVK}
        for k in _ENVK:
            os.environ.pop(k, None)
        _VAULT_STORE.clear()
        _VAULT_CALLS.clear()
        # gateway() now consults channels/ag2space/.env BEFORE the vault, so on a
        # host that has one the vault tier is never reached. Shadow it like above.
        # Plain callable, NOT staticmethod(...): _gateway is a module, and before
        # 3.10 a staticmethod object is not callable — the TypeError would be
        # swallowed as "no channel tier", so every vault case below would reach
        # the vault through the error path instead of the absence path.
        self._shadow_calls = []

        def _no_channel_file():
            self._shadow_calls.append(1)
            return None

        _p = mock.patch.object(_gateway, "_channel_env_file", _no_channel_file)
        _p.start()
        self.addCleanup(_p.stop)

    def assert_shadow_was_invoked(self):
        """Call/effect control: absence must come from the shadow RUNNING."""
        self.assertTrue(self._shadow_calls,
                        "shadow never invoked - vault was reached via an error "
                        "path, not the channel-absent path")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        _VAULT_STORE.clear()

    # ---- resolver (injected vault_get — pins the shipped policy) ---- #
    def test_resolves_current_name(self):
        vault = {"REMOTE_TASK_TOKEN": "https://gw.example/relay|s3cr3t"}
        self.assertEqual(
            _gateway._token_from_vault(vault_get=lambda k: vault.get(k)),
            "https://gw.example/relay|s3cr3t")


    def test_falls_back_to_legacy_alias(self):
        vault = {"AG2_REMOTE_TOKEN": "legacy-secret"}
        self.assertEqual(
            _gateway._token_from_vault(vault_get=lambda k: vault.get(k)), "legacy-secret")

    def test_total_failure_safe(self):
        self.assertEqual(_gateway._token_from_vault(vault_get=lambda k: None), "")

        def boom(k):
            raise RuntimeError("keychain locked")

        self.assertEqual(_gateway._token_from_vault(vault_get=boom), "")

    def test_degrades_when_core_absent(self):
        real = os.path.isfile
        os.path.isfile = lambda p: False   # channel_token.py "not found" (standalone)
        try:
            self.assertEqual(_gateway._token_from_vault(vault_get=lambda k: "x"), "")
        finally:
            os.path.isfile = real

    def test_safe_on_broken_core_import(self):
        broken = types.ModuleType("channel_token")
        saved = sys.modules.get("channel_token")
        sys.modules["channel_token"] = broken
        try:
            self.assertEqual(_gateway._token_from_vault(vault_get=lambda k: "x"), "")
        finally:
            if saved is not None:
                sys.modules["channel_token"] = saved
            else:
                sys.modules.pop("channel_token", None)

    # ---- gateway() integration ---- #
    def test_vault_combined_token_arms_base_and_bearer(self):
        with mock.patch.object(
                _gateway, "_token_from_vault",
                return_value="https://gw.example/relay|s3cr3t"):
            base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gw.example/relay")
        self.assertEqual(headers.get("Authorization"), "Bearer s3cr3t")

    def test_env_token_wins_over_vault(self):
        os.environ["GATEWAY_TOKEN"] = "env-secret"
        with mock.patch.object(
                _gateway, "_token_from_vault", return_value="vault-should-not-win") as m:
            _base, headers = _gateway.gateway()
        self.assertEqual(headers.get("Authorization"), "Bearer env-secret")
        m.assert_not_called()

    # ---- hermeticity controls (@john-the-dev / @qingyun-air) ---- #
    def test_gateway_empty_env_makes_zero_host_vault_calls(self):
        base, headers = _gateway.gateway()   # empty env (setUp) -> vault fallthrough
        self.assertIs(sys.modules["vault_intercept"], _FAKE_VI)   # host boundary replaced
        self.assertIn("REMOTE_TASK_TOKEN", _VAULT_CALLS)          # seam WAS the shadow
        self.assertEqual(base, "")
        self.assertNotIn("Authorization", headers)

    def test_gateway_resolves_a_stored_vault_token_through_the_shadow(self):
        _VAULT_STORE["REMOTE_TASK_TOKEN"] = "https://gw.example/relay|from-vault"
        base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gw.example/relay")
        self.assertEqual(headers.get("Authorization"), "Bearer from-vault")
        # The vault must be reached because the channel file is ABSENT, not
        # because reading it raised and the guard swallowed it.
        self.assert_shadow_was_invoked()


if __name__ == "__main__":
    unittest.main(verbosity=2)
