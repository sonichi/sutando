#!/usr/bin/env python3
"""Tests for the vault DELETE verb — vault_intercept.delete_vault_key and the
secret-vault.py `delete` CLI subcommand.

The `security` Keychain subprocess is STUBBED throughout (an in-memory dict
stands in for the real Keychain), mirroring tests/vault-intercept.test.py — no
real `security` process is spawned and no secret touches the runner's Keychain.
The manifest is redirected to a temp file (both the canonical and legacy paths),
so registering/deregistering key NAMES never writes the real
`<workspace>/state/secret-vault/keys.json`.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

# src/ on path for vault_intercept.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import vault_intercept

# secret-vault.py is hyphenated — load via importlib and register as "vault"
# so patch("vault.*") strings resolve (same trick as vault-skill.test.py).
_SV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "skills", "secret-vault", "secret-vault.py"
)
_spec = importlib.util.spec_from_file_location("vault", _SV_PATH)
vault_cli = importlib.util.module_from_spec(_spec)
sys.modules["vault"] = vault_cli
_spec.loader.exec_module(vault_cli)


def _fake_security(keychain: dict):
    """A stand-in for `subprocess.run(["security", ...])` backed by `keychain`.

    Implements the three verbs the vault touches — add/find/delete
    generic-password — so a set→delete round trip is observable end-to-end
    without a real Keychain. `delete` of an absent item returns non-zero
    (errSecItemNotFound, 44), exactly as the real `security` does; that is the
    case delete_vault_key must treat as idempotent success.
    """
    def _run(cmd, **kw):
        verb = cmd[1] if len(cmd) > 1 else ""
        key = cmd[cmd.index("-s") + 1] if "-s" in cmd else None
        if verb == "add-generic-password":
            keychain[key] = cmd[cmd.index("-w") + 1]
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if verb == "find-generic-password":
            if key in keychain:
                return MagicMock(returncode=0, stdout=(keychain[key] + "\n").encode(), stderr=b"")
            return MagicMock(returncode=44, stdout=b"", stderr=b"not found")
        if verb == "delete-generic-password":
            if key in keychain:
                del keychain[key]
                return MagicMock(returncode=0, stdout=b"", stderr=b"")
            return MagicMock(returncode=44, stdout=b"", stderr=b"not found")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")
    return _run


class _FakeVaultBase(unittest.TestCase):
    """Redirects the manifest to a temp file and stubs `security` with an
    in-memory Keychain for the duration of each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vault-delete-test-")
        fake_manifest = os.path.join(self._tmp.name, "state", "secret-vault", "keys.json")
        self.keychain: dict[str, str] = {}
        self._patches = [
            patch.object(vault_intercept, "_manifest_path", return_value=fake_manifest),
            # _read_manifest consults the legacy path directly, so redirect it too.
            patch.object(vault_intercept, "_LEGACY_MANIFEST_PATH", fake_manifest),
            patch("vault_intercept.subprocess.run", side_effect=_fake_security(self.keychain)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


# --- vault_intercept.delete_vault_key — the helper ---


class TestDeleteVaultKeyHelper(_FakeVaultBase):
    def test_delete_removes_a_set_key(self):
        # Set it: list shows it, get returns it.
        vault_intercept.set_vault_key("MY_KEY", "supersecret")
        self.assertIn("MY_KEY", vault_intercept.list_vault_keys())
        self.assertEqual(vault_intercept.get_vault_key("MY_KEY"), "supersecret")

        # Delete it: list no longer shows it, get now fails, Keychain item gone.
        vault_intercept.delete_vault_key("MY_KEY")
        self.assertNotIn("MY_KEY", vault_intercept.list_vault_keys())
        self.assertNotIn("MY_KEY", self.keychain)
        with self.assertRaises(KeyError):
            vault_intercept.get_vault_key("MY_KEY")

    def test_delete_absent_key_is_success(self):
        # Deleting a never-stored key raises nothing — the idempotent contract
        # the desktop teardown relies on when it re-runs.
        vault_intercept.delete_vault_key("NEVER_STORED")
        self.assertEqual(vault_intercept.list_vault_keys(), [])

    def test_half_state_manifest_ghost_is_reconciled(self):
        # Manifest-registered key whose Keychain item is gone (the "ghost").
        # delete must scrub the manifest, not error on the missing item.
        vault_intercept._register_key("GHOST")
        self.assertIn("GHOST", vault_intercept.list_vault_keys())
        self.assertNotIn("GHOST", self.keychain)  # no Keychain half

        vault_intercept.delete_vault_key("GHOST")
        self.assertNotIn("GHOST", vault_intercept.list_vault_keys())

    def test_half_state_orphan_keychain_item_is_reconciled(self):
        # Mirror image: Keychain item, no manifest entry. delete must remove the
        # item (a later get fails) without erroring on the absent manifest entry.
        self.keychain["ORPHAN"] = "leftover"
        self.assertNotIn("ORPHAN", vault_intercept.list_vault_keys())

        vault_intercept.delete_vault_key("ORPHAN")
        self.assertNotIn("ORPHAN", self.keychain)
        with self.assertRaises(KeyError):
            vault_intercept.get_vault_key("ORPHAN")

    def test_deletes_both_halves_together(self):
        # Both halves present → both gone after delete (not a half-delete).
        vault_intercept.set_vault_key("BOTH", "v")
        vault_intercept.delete_vault_key("BOTH")
        self.assertNotIn("BOTH", self.keychain)
        self.assertNotIn("BOTH", vault_intercept.list_vault_keys())

    def test_idempotent_double_delete(self):
        vault_intercept.set_vault_key("TWICE", "v")
        vault_intercept.delete_vault_key("TWICE")
        # Second delete on the now-absent key is still success.
        vault_intercept.delete_vault_key("TWICE")
        self.assertNotIn("TWICE", vault_intercept.list_vault_keys())


class TestDeleteKeyNameValidation(_FakeVaultBase):
    """Key-name validation is UNCHANGED from set_vault_key — same rule, not
    loosened. The bad/good sets mirror TestSetVaultKey in vault-skill.test.py."""

    _BAD_KEYS = ("", "9LEADING", "has space", "has-dash", "a=b", "k\nnewline")

    def test_rejects_the_same_invalid_key_names_as_set(self):
        for bad in self._BAD_KEYS:
            with self.assertRaises(ValueError):
                vault_intercept.delete_vault_key(bad)

    def test_invalid_key_never_touches_keychain(self):
        # Validation fails BEFORE any `security` call — a bad key can't reach
        # the delete subprocess.
        for bad in self._BAD_KEYS:
            with patch("vault_intercept.subprocess.run") as mock_run:
                with self.assertRaises(ValueError):
                    vault_intercept.delete_vault_key(bad)
                mock_run.assert_not_called()

    def test_delete_and_set_agree_on_validity(self):
        # Prove the two verbs share the exact rule: for every bad name, both
        # raise ValueError; a representative valid name is accepted by both.
        for bad in self._BAD_KEYS:
            with self.assertRaises(ValueError):
                vault_intercept.set_vault_key(bad, "v")
            with self.assertRaises(ValueError):
                vault_intercept.delete_vault_key(bad)
        # Valid name: set stores, delete removes — neither raises.
        vault_intercept.set_vault_key("VALID_KEY", "v")
        vault_intercept.delete_vault_key("VALID_KEY")


# --- secret-vault.py — the `delete` CLI subcommand ---


class TestVaultCliDelete(unittest.TestCase):
    def test_prints_deleted_on_success(self):
        with patch("vault.delete_vault_key") as mock_del:
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_delete("MY_KEY")
        mock_del.assert_called_once_with("MY_KEY")
        self.assertIn("deleted 'MY_KEY'", buf.getvalue())

    def test_exits_1_on_invalid_key(self):
        with patch("vault.delete_vault_key", side_effect=ValueError("bad key")):
            with self.assertRaises(SystemExit) as cm:
                with redirect_stderr(io.StringIO()):
                    vault_cli.cmd_delete("bad key")
        self.assertEqual(cm.exception.code, 1)

    def test_absent_key_exits_0(self):
        # CLI teardown contract: deleting an absent key exits 0 (no SystemExit).
        # Helper stubbed to a no-op success, standing in for an already-gone key.
        with patch("vault.delete_vault_key", return_value=None):
            with patch("vault.sys.argv", ["secret-vault.py", "delete", "GONE_KEY"]):
                with redirect_stdout(io.StringIO()):
                    vault_cli.main()  # no SystemExit → exit 0


class TestVaultCliDeleteDispatch(unittest.TestCase):
    def _main(self, argv):
        with patch("vault.sys.argv", ["secret-vault.py", *argv]):
            vault_cli.main()

    def test_delete_dispatches_to_cmd_delete(self):
        with patch("vault.cmd_delete") as mock_cmd:
            self._main(["delete", "MY_KEY"])
        mock_cmd.assert_called_once_with("MY_KEY")

    def test_delete_missing_key_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(io.StringIO()):
                self._main(["delete"])
        self.assertEqual(cm.exception.code, 1)

    def test_usage_line_mentions_delete(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(buf):
            self._main(["frobnicate"])
        self.assertIn("delete KEY", buf.getvalue())


class TestKeychainDeleteFailure(_FakeVaultBase):
    """rc 44 means already-absent and is success; any OTHER non-zero may leave the
    item live, so the manifest entry must survive rather than strand the secret."""

    def test_a_locked_keychain_raises_and_keeps_the_manifest_entry(self):
        vault_intercept.set_vault_key("LOCKED_KEY", "still-live")
        # errSecInteractionNotAllowed: the launchd-teardown case. The 0/44 stub
        # cannot produce it, which is why nothing previously exercised this path.
        with patch("vault_intercept.subprocess.run",
                   return_value=MagicMock(returncode=25308, stdout=b"", stderr=b"locked")):
            with self.assertRaises(RuntimeError):
                vault_intercept.delete_vault_key("LOCKED_KEY")
        self.assertIn("LOCKED_KEY", vault_intercept.list_vault_keys(),
                      "a failed Keychain delete must not deregister the key")

    def test_item_not_found_is_still_success(self):
        # The discriminating control: same code path, rc 44 instead, still deletes.
        vault_intercept.set_vault_key("GONE_KEY", "x")
        with patch("vault_intercept.subprocess.run",
                   return_value=MagicMock(returncode=44, stdout=b"", stderr=b"not found")):
            vault_intercept.delete_vault_key("GONE_KEY")
        self.assertNotIn("GONE_KEY", vault_intercept.list_vault_keys())


if __name__ == "__main__":
    unittest.main()
