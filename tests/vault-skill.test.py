"""Tests for skills/secret-vault/secret-vault.py and the extended vault_intercept helpers."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, mock_open, patch

# Ensure src/ is on path for vault_intercept
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import vault_intercept

# skills/secret-vault/secret-vault.py (renamed from skills/vault/vault.py) has a
# hyphenated filename, so it can't be imported by name. Load it via importlib and
# register under "vault" so the patch("vault.*") strings below keep resolving.
import importlib.util
_SV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "skills", "secret-vault", "secret-vault.py"
)
_spec = importlib.util.spec_from_file_location("vault", _SV_PATH)
vault_cli = importlib.util.module_from_spec(_spec)
sys.modules["vault"] = vault_cli
_spec.loader.exec_module(vault_cli)


# ---------------------------------------------------------------------------
# vault_intercept — manifest / registry helpers
# ---------------------------------------------------------------------------


class TestListVaultKeys(unittest.TestCase):
    def test_returns_sorted_keys(self):
        manifest = {"ZEBRA": {"stored_at": "x"}, "ALPHA": {"stored_at": "y"}}
        with patch.object(vault_intercept, "_read_manifest", return_value=manifest):
            result = vault_intercept.list_vault_keys()
        self.assertEqual(result, ["ALPHA", "ZEBRA"])

    def test_empty_when_manifest_empty(self):
        with patch.object(vault_intercept, "_read_manifest", return_value={}):
            result = vault_intercept.list_vault_keys()
        self.assertEqual(result, [])


class TestReadManifest(unittest.TestCase):
    """Read-error handling + canonical-preferred / legacy-fallback resolution."""

    def _patch_paths(self, canonical, legacy):
        return (
            patch.object(vault_intercept, "_manifest_path", return_value=canonical),
            patch.object(vault_intercept, "_LEGACY_MANIFEST_PATH", legacy),
        )

    def test_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = self._patch_paths(os.path.join(tmp, "c.json"), os.path.join(tmp, "l.json"))
            with p1, p2:
                self.assertEqual(vault_intercept._read_manifest(), {})

    def test_corrupt_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = os.path.join(tmp, "c.json")
            with open(canonical, "w") as f:
                f.write("not-json")
            p1, p2 = self._patch_paths(canonical, os.path.join(tmp, "l.json"))
            with p1, p2:
                self.assertEqual(vault_intercept._read_manifest(), {})

    def test_prefers_canonical_over_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = os.path.join(tmp, "c.json")
            legacy = os.path.join(tmp, "l.json")
            with open(canonical, "w") as f:
                json.dump({"NEW": {"stored_at": "t"}}, f)
            with open(legacy, "w") as f:
                json.dump({"OLD": {"stored_at": "t"}}, f)
            p1, p2 = self._patch_paths(canonical, legacy)
            with p1, p2:
                self.assertEqual(sorted(vault_intercept._read_manifest()), ["NEW"])

    def test_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = os.path.join(tmp, "c.json")  # absent
            legacy = os.path.join(tmp, "l.json")
            with open(legacy, "w") as f:
                json.dump({"OLD": {"stored_at": "t"}}, f)
            p1, p2 = self._patch_paths(canonical, legacy)
            with p1, p2:
                self.assertEqual(sorted(vault_intercept._read_manifest()), ["OLD"])


class TestGetVaultKey(unittest.TestCase):
    def test_returns_value_on_success(self):
        mock_result = MagicMock(returncode=0, stdout=b"supersecret\n")
        with patch("subprocess.run", return_value=mock_result):
            val = vault_intercept.get_vault_key("MY_KEY")
        self.assertEqual(val, "supersecret")

    def test_raises_key_error_when_not_found(self):
        mock_result = MagicMock(returncode=44, stdout=b"", stderr=b"not found")
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(KeyError):
                vault_intercept.get_vault_key("MISSING")


class TestRegisterKey(unittest.TestCase):
    def _patch_paths(self, canonical, legacy):
        return (
            patch.object(vault_intercept, "_manifest_path", return_value=canonical),
            patch.object(vault_intercept, "_LEGACY_MANIFEST_PATH", legacy),
        )

    def test_new_key_written_to_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "keys.json")
            p1, p2 = self._patch_paths(manifest_path, os.path.join(tmpdir, "legacy.json"))
            with p1, p2:
                vault_intercept._register_key("NEW_KEY")
            with open(manifest_path) as f:
                data = json.load(f)
            self.assertIn("NEW_KEY", data)
            self.assertIn("stored_at", data["NEW_KEY"])

    def test_existing_key_updated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "keys.json")
            with open(manifest_path, "w") as f:
                json.dump({"OLD": {"stored_at": "2020"}}, f)
            p1, p2 = self._patch_paths(manifest_path, os.path.join(tmpdir, "legacy.json"))
            with p1, p2:
                vault_intercept._register_key("OLD")
            with open(manifest_path) as f:
                data = json.load(f)
            self.assertIn("OLD", data)
            self.assertNotEqual(data["OLD"]["stored_at"], "2020")

    def test_first_write_migrates_legacy_keys(self):
        """Canonical absent + legacy present → first write inherits legacy keys
        (and creates the nested canonical dir)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical = os.path.join(tmpdir, "state", "secret-vault", "keys.json")  # nested, absent
            legacy = os.path.join(tmpdir, "legacy.json")
            with open(legacy, "w") as f:
                json.dump({"A": {"stored_at": "t"}, "B": {"stored_at": "t"}}, f)
            p1, p2 = self._patch_paths(canonical, legacy)
            with p1, p2:
                vault_intercept._register_key("C")
            with open(canonical) as f:
                data = json.load(f)
            self.assertEqual(sorted(data), ["A", "B", "C"])


class TestStoreRegistersKey(unittest.TestCase):
    def test_successful_store_calls_register(self):
        mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=mock_result), \
             patch.object(vault_intercept, "_register_key") as mock_reg:
            vault_intercept._store_in_keychain("FOO", "bar")
        mock_reg.assert_called_once_with("FOO")

    def test_failed_store_does_not_register(self):
        mock_result = MagicMock(returncode=1, stdout=b"", stderr=b"err")
        with patch("subprocess.run", return_value=mock_result), \
             patch.object(vault_intercept, "_register_key") as mock_reg:
            with self.assertRaises(RuntimeError):
                vault_intercept._store_in_keychain("FOO", "bar")
        mock_reg.assert_not_called()


# ---------------------------------------------------------------------------
# vault CLI subcommands
# ---------------------------------------------------------------------------


import io
from contextlib import redirect_stdout


class TestVaultCliList(unittest.TestCase):
    def test_prints_keys(self):
        with patch("vault.list_vault_keys", return_value=["ALPHA", "BETA"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_list()
        self.assertIn("ALPHA", buf.getvalue())
        self.assertIn("BETA", buf.getvalue())

    def test_prints_empty_message_when_no_keys(self):
        with patch("vault.list_vault_keys", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_list()
        self.assertIn("no keys", buf.getvalue())


class TestVaultCliGet(unittest.TestCase):
    def test_prints_value(self):
        with patch("vault.get_vault_key", return_value="secret123"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_get("MY_KEY")
        self.assertEqual(buf.getvalue().strip(), "secret123")

    def test_exits_1_on_missing_key(self):
        with patch("vault.get_vault_key", side_effect=KeyError("not found")):
            with self.assertRaises(SystemExit) as cm:
                vault_cli.cmd_get("MISSING")
        self.assertEqual(cm.exception.code, 1)


class TestVaultCliEnv(unittest.TestCase):
    def test_injects_env_and_runs(self):
        with patch("vault.get_vault_key", return_value="val"), \
             patch("vault.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            with self.assertRaises(SystemExit) as cm:
                vault_cli.cmd_env(["MY_KEY"], ["echo", "hi"])
        self.assertEqual(cm.exception.code, 0)
        env_passed = mock_run.call_args[1]["env"]
        self.assertEqual(env_passed["MY_KEY"], "val")

    def test_exits_1_on_missing_key(self):
        with patch("vault.get_vault_key", side_effect=KeyError("x")):
            with self.assertRaises(SystemExit) as cm:
                vault_cli.cmd_env(["MISSING"], ["echo", "hi"])
        self.assertEqual(cm.exception.code, 1)

    def test_exits_1_on_empty_cmd(self):
        with self.assertRaises(SystemExit) as cm:
            vault_cli.cmd_env(["KEY"], [])
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
