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
case. Hermetic: a fake `vault_get`, never a real Keychain; isolates
CLAUDE_CONFIG_DIR per the bridge-test lint.

Run: python3 packages/ag2-sparrow/tests/test_vault_token_tier.py
"""
import os
import sys
import types
import tempfile
import importlib
import pathlib


def _load(tmp):
    # Hermetic: no host config is read at import (bridge reads config during
    # exec_module — see lint-hermetic-bridge-tests). Clear every token source so
    # the module-level resolution reaches the vault tier under test.
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    for k in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_URL",
              "AG2_REMOTE_URL", "AG2_DEVICE_ENV", "GATEWAY_INSTANCE"):
        os.environ.pop(k, None)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def test_vault_tier_resolves_current_name():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        vault = {"REMOTE_TASK_TOKEN": "https://gw.example/relay|s3cr3t"}
        got = m._token_from_vault_ag2space(vault_get=lambda k: vault.get(k))
        assert got == "https://gw.example/relay|s3cr3t", got
        print("PASS resolves REMOTE_TASK_TOKEN from vault")


def test_vault_tier_falls_back_to_legacy_alias():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        vault = {"AG2_REMOTE_TOKEN": "legacy-secret"}   # only the legacy name set
        got = m._token_from_vault_ag2space(vault_get=lambda k: vault.get(k))
        assert got == "legacy-secret", got
        print("PASS falls back to legacy AG2_REMOTE_TOKEN")


def test_vault_tier_total_failure_safe():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        # empty vault -> '' ; a raising vault_get -> '' (never propagates out)
        assert m._token_from_vault_ag2space(vault_get=lambda k: None) == ""

        def boom(k):
            raise RuntimeError("keychain locked")

        assert m._token_from_vault_ag2space(vault_get=boom) == ""
        print("PASS total-failure-safe (empty + raising vault)")


def test_vault_tier_degrades_when_core_absent():
    # Standalone-install case: the monorepo src/channel_token is not locatable.
    # The tier must degrade to '' (pre-#2638 behavior), never crash the bridge.
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        real_isfile = os.path.isfile
        os.path.isfile = lambda p: False   # channel_token.py "not found"
        try:
            got = m._token_from_vault_ag2space(vault_get=lambda k: "would-be-token")
        finally:
            os.path.isfile = real_isfile
        assert got == "", got
        print("PASS degrades to '' when core policy absent (standalone install)")


def test_vault_tier_safe_on_broken_core_import():
    # A present-but-incompatible channel_token (no token_from_vault symbol) must
    # be caught, not propagated — the bridge must still start.
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
        assert got == "", got
        print("PASS safe on broken/incompatible core import")


if __name__ == "__main__":
    test_vault_tier_resolves_current_name()
    test_vault_tier_falls_back_to_legacy_alias()
    test_vault_tier_total_failure_safe()
    test_vault_tier_degrades_when_core_absent()
    test_vault_tier_safe_on_broken_core_import()
    print("\nall passed")
