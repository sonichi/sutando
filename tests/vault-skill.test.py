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


class TestSetVaultKey(unittest.TestCase):
    def test_delegates_to_store(self):
        with patch.object(vault_intercept, "_store_in_keychain") as mock_store:
            vault_intercept.set_vault_key("MY_KEY", "supersecret")
        mock_store.assert_called_once_with("MY_KEY", "supersecret")

    def test_rejects_invalid_key_names(self):
        for bad in ("", "9LEADING", "has space", "has-dash", "a=b", "k\nnewline"):
            with patch.object(vault_intercept, "_store_in_keychain") as mock_store:
                with self.assertRaises(ValueError):
                    vault_intercept.set_vault_key(bad, "value")
            mock_store.assert_not_called()

    def test_rejects_empty_value(self):
        with patch.object(vault_intercept, "_store_in_keychain") as mock_store:
            with self.assertRaises(ValueError):
                vault_intercept.set_vault_key("MY_KEY", "")
        mock_store.assert_not_called()


class TestDeleteVaultKey(unittest.TestCase):
    def test_deletes_from_keychain_and_manifest(self):
        mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch.object(vault_intercept, "_read_manifest", return_value={"MY_KEY": {}}), \
             patch.object(vault_intercept, "_deregister_key") as mock_dereg:
            vault_intercept.delete_vault_key("MY_KEY")
        self.assertIn("delete-generic-password", mock_run.call_args[0][0])
        mock_dereg.assert_called_once_with("MY_KEY")

    def test_manifest_only_entry_still_deregisters(self):
        # Keychain item already gone (drift) — the manifest entry is cleaned up
        # rather than erroring.
        mock_result = MagicMock(returncode=44, stdout=b"", stderr=b"not found")
        with patch("subprocess.run", return_value=mock_result), \
             patch.object(vault_intercept, "_read_manifest", return_value={"MY_KEY": {}}), \
             patch.object(vault_intercept, "_deregister_key") as mock_dereg:
            vault_intercept.delete_vault_key("MY_KEY")
        mock_dereg.assert_called_once_with("MY_KEY")

    def test_raises_key_error_when_nowhere(self):
        mock_result = MagicMock(returncode=44, stdout=b"", stderr=b"not found")
        with patch("subprocess.run", return_value=mock_result), \
             patch.object(vault_intercept, "_read_manifest", return_value={}), \
             patch.object(vault_intercept, "_deregister_key") as mock_dereg:
            with self.assertRaises(KeyError):
                vault_intercept.delete_vault_key("MISSING")
        mock_dereg.assert_not_called()

    def test_rejects_invalid_key_names(self):
        for bad in ("", "has space", "has-dash", "a=b"):
            with patch("subprocess.run") as mock_run:
                with self.assertRaises(ValueError):
                    vault_intercept.delete_vault_key(bad)
            mock_run.assert_not_called()


class TestDeregisterKey(unittest.TestCase):
    def test_removes_key_and_rewrites_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "keys.json")
            with open(path, "w") as f:
                json.dump({"KEEP": {"stored_at": "x"}, "DROP": {"stored_at": "y"}}, f)
            with patch.object(vault_intercept, "_manifest_path", return_value=path), \
                 patch.object(vault_intercept, "_read_manifest",
                              return_value={"KEEP": {"stored_at": "x"}, "DROP": {"stored_at": "y"}}):
                vault_intercept._deregister_key("DROP")
            with open(path) as f:
                manifest = json.load(f)
        self.assertEqual(list(manifest.keys()), ["KEEP"])

    def test_absent_key_is_a_noop_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "keys.json")
            with patch.object(vault_intercept, "_manifest_path", return_value=path), \
                 patch.object(vault_intercept, "_read_manifest", return_value={"KEEP": {}}):
                vault_intercept._deregister_key("NOT_THERE")
            # No manifest file written for a no-op deregister.
            self.assertFalse(os.path.exists(path))


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
from contextlib import redirect_stderr, redirect_stdout


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


class TestVaultCliDelete(unittest.TestCase):
    def test_prints_confirmation(self):
        with patch("vault.delete_vault_key") as mock_del:
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_delete("MY_KEY")
        mock_del.assert_called_once_with("MY_KEY")
        self.assertIn("deleted 'MY_KEY'", buf.getvalue())

    def test_exits_1_on_missing_key(self):
        with patch("vault.delete_vault_key", side_effect=KeyError("not found")):
            with self.assertRaises(SystemExit) as cm:
                vault_cli.cmd_delete("MISSING")
        self.assertEqual(cm.exception.code, 1)


class TestVaultCliSet(unittest.TestCase):
    def test_reads_stdin_and_strips_one_trailing_newline(self):
        with patch("vault.set_vault_key") as mock_set, \
             patch("vault.sys.stdin", io.StringIO("secret123\n")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                vault_cli.cmd_set("MY_KEY")
        mock_set.assert_called_once_with("MY_KEY", "secret123")
        self.assertIn("stored 'MY_KEY'", buf.getvalue())

    def test_preserves_inner_whitespace_and_extra_newlines(self):
        # Only the single trailing newline is stripped; everything else is data.
        with patch("vault.set_vault_key") as mock_set, \
             patch("vault.sys.stdin", io.StringIO("line1\nline2\n\n")):
            with redirect_stdout(io.StringIO()):
                vault_cli.cmd_set("MY_KEY")
        mock_set.assert_called_once_with("MY_KEY", "line1\nline2\n")

    def test_exits_1_on_setter_error(self):
        with patch("vault.set_vault_key", side_effect=ValueError("bad key")), \
             patch("vault.sys.stdin", io.StringIO("v\n")):
            with self.assertRaises(SystemExit) as cm:
                vault_cli.cmd_set("bad key")
        self.assertEqual(cm.exception.code, 1)


class TestVaultCliMainDispatch(unittest.TestCase):
    """main()'s set-dispatch branches (argv guards live here, not in cmd_set)."""

    def _main(self, argv):
        with patch("vault.sys.argv", ["secret-vault.py", *argv]):
            vault_cli.main()

    def test_set_dispatches_to_cmd_set(self):
        with patch("vault.cmd_set") as mock_cmd:
            self._main(["set", "MY_KEY"])
        mock_cmd.assert_called_once_with("MY_KEY")

    def test_set_missing_key_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            self._main(["set"])
        self.assertEqual(cm.exception.code, 1)

    def test_set_refuses_value_on_argv(self):
        # The whole point of the stdin design — a value on argv is refused, not stored.
        with patch("vault.cmd_set") as mock_cmd:
            with self.assertRaises(SystemExit) as cm:
                self._main(["set", "MY_KEY", "leaky-value"])
        self.assertEqual(cm.exception.code, 1)
        mock_cmd.assert_not_called()

    def test_unknown_subcommand_usage_mentions_set(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(buf):
            self._main(["frobnicate"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("set KEY", buf.getvalue())


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


class TestVaultCliDeleteErrorBranches(unittest.TestCase):
    """Error/exit branches of the delete verb (coverage-gate follow-up,
    john-the-dev peer note 2026-07-20): the happy path is covered above;
    these pin the failure contract — exit 1 + the error on stderr."""

    def _main(self, argv):
        with patch.object(sys, "argv", ["secret-vault.py", *argv]):
            vault_cli.main()

    def test_delete_missing_key_exits_1_with_stderr(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with patch("vault.delete_vault_key", side_effect=KeyError("vault: key 'NOPE' not found")):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(buf):
                self._main(["delete", "NOPE"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("NOPE", buf.getvalue())

    def test_delete_invalid_name_exits_1(self):
        with patch("vault.delete_vault_key", side_effect=ValueError("vault: invalid key name")):
            with self.assertRaises(SystemExit) as cm:
                self._main(["delete", "bad-name"])
        self.assertEqual(cm.exception.code, 1)

    def test_delete_without_key_exits_1_with_usage(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(buf):
            self._main(["delete"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("missing KEY", buf.getvalue())


class TestDeleteVaultKeyErrorBranches(unittest.TestCase):
    """vault_intercept.delete_vault_key / _deregister_key error + no-op paths."""

    def test_not_found_anywhere_raises_keyerror(self):
        # Keychain says no (rc!=0) AND manifest doesn't have it -> KeyError.
        with patch.object(vault_intercept.subprocess, "run",
                          return_value=MagicMock(returncode=44)), \
             patch.object(vault_intercept, "_read_manifest", return_value={}):
            with self.assertRaises(KeyError):
                vault_intercept.delete_vault_key("GHOST_KEY")

    def test_invalid_key_name_raises_valueerror(self):
        with self.assertRaises(ValueError):
            vault_intercept.delete_vault_key("not a valid name!")

    def test_manifest_only_drift_still_deletes(self):
        # Keychain item already gone but manifest has the entry: tolerated,
        # deregisters instead of erroring (the drift-cleanup contract).
        with patch.object(vault_intercept.subprocess, "run",
                          return_value=MagicMock(returncode=44)), \
             patch.object(vault_intercept, "_read_manifest",
                          return_value={"DRIFTED": {"stored_at": "x"}}), \
             patch.object(vault_intercept, "_deregister_key") as mock_dereg:
            vault_intercept.delete_vault_key("DRIFTED")
        mock_dereg.assert_called_once_with("DRIFTED")

    def test_real_security_failure_raises_and_keeps_manifest(self):
        # P1 regression (Codex 2026-07-20): a NON-not-found `security` failure
        # (locked Keychain, user denial — rc != 44) while the manifest still
        # holds the key must RAISE and leave the manifest untouched. Anything
        # else silently hides a live credential from `list`.
        with patch.object(vault_intercept.subprocess, "run",
                          return_value=MagicMock(returncode=51, stderr=b"User interaction is not allowed.")), \
             patch.object(vault_intercept, "_read_manifest",
                          return_value={"LOCKED": {"stored_at": "x"}}), \
             patch.object(vault_intercept, "_deregister_key") as mock_dereg:
            with self.assertRaises(RuntimeError):
                vault_intercept.delete_vault_key("LOCKED")
        mock_dereg.assert_not_called()

    def test_deregister_missing_key_is_noop(self):
        # _deregister_key early-returns when the key isn't in the manifest —
        # no write attempted.
        with patch.object(vault_intercept, "_read_manifest", return_value={}), \
             patch.object(vault_intercept, "_manifest_path") as mock_path:
            vault_intercept._deregister_key("ABSENT")
        mock_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
