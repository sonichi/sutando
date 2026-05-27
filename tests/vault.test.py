#!/usr/bin/env python3
"""Tests for skills/vault/vault.py

Runs without touching the real macOS Keychain — all `security` calls are
mocked. Tests cover the CLI parsing layer, the redact_task_file helper,
and the error-exit paths.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vault = _load("vault", REPO / "skills" / "vault" / "vault.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout="", returncode=0):
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = ""
    return r


def _err(stderr="", returncode=1):
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = ""
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# vault_set
# ---------------------------------------------------------------------------

class TestVaultSet(unittest.TestCase):
    def test_calls_security_add(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok()) as mock_run:
            vault.vault_set("MY_KEY", "secret123")
        args = mock_run.call_args[0][0]
        self.assertIn("add-generic-password", args)
        self.assertIn("MY_KEY", args)
        self.assertIn("secret123", args)
        self.assertIn("-U", args)  # update-or-create flag

    def test_exits_on_failure(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_err("permission denied", 1)):
            with self.assertRaises(SystemExit):
                vault.vault_set("MY_KEY", "val")

    def test_account_is_sutando(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok()) as mock_run:
            vault.vault_set("X", "y")
        args = mock_run.call_args[0][0]
        idx = args.index("-a")
        self.assertEqual(args[idx + 1], "sutando")


# ---------------------------------------------------------------------------
# vault_get
# ---------------------------------------------------------------------------

class TestVaultGet(unittest.TestCase):
    def test_returns_and_prints_value(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok("sk_abc\n")):
            with unittest.mock.patch("builtins.print") as mock_print:
                result = vault.vault_get("MY_KEY")
        self.assertEqual(result, "sk_abc")
        mock_print.assert_called_once_with("sk_abc")

    def test_exits_if_not_found(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_err("", 44)):
            with self.assertRaises(SystemExit):
                vault.vault_get("MISSING_KEY")

    def test_strips_trailing_newline(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok("token\n\n")):
            with unittest.mock.patch("builtins.print"):
                result = vault.vault_get("K")
        self.assertEqual(result, "token")


# ---------------------------------------------------------------------------
# vault_list
# ---------------------------------------------------------------------------

class TestVaultList(unittest.TestCase):
    def _make_keychain_dump(self, entries):
        """Build a minimal `security dump-keychain` output for given (account, service) pairs."""
        lines = []
        for acct, svc in entries:
            lines.append(f'    "acct"<blob>="{acct}"')
            lines.append(f'    "svce"<blob>="{svc}"')
        return "\n".join(lines)

    def test_lists_sutando_keys(self):
        dump = self._make_keychain_dump([
            ("sutando", "APOLLO_KEY"),
            ("sutando", "HUNTER_KEY"),
            ("other-account", "NOISE"),
        ])
        with unittest.mock.patch.object(vault, "_run", return_value=_ok(dump)):
            with unittest.mock.patch("builtins.print") as mock_print:
                keys = vault.vault_list()
        printed = [call[0][0] for call in mock_print.call_args_list]
        self.assertIn("APOLLO_KEY", printed)
        self.assertIn("HUNTER_KEY", printed)
        self.assertNotIn("NOISE", printed)
        self.assertEqual(set(keys), {"APOLLO_KEY", "HUNTER_KEY"})

    def test_empty_keychain_prints_notice(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok("")):
            with unittest.mock.patch("builtins.print") as mock_print:
                keys = vault.vault_list()
        self.assertEqual(keys, [])
        mock_print.assert_called_once_with("(no keys stored)")


# ---------------------------------------------------------------------------
# vault_delete
# ---------------------------------------------------------------------------

class TestVaultDelete(unittest.TestCase):
    def test_calls_delete_generic_password(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok()) as mock_run:
            vault.vault_delete("OLD_KEY")
        args = mock_run.call_args[0][0]
        self.assertIn("delete-generic-password", args)
        self.assertIn("OLD_KEY", args)

    def test_exits_if_not_found(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_err("", 44)):
            with self.assertRaises(SystemExit):
                vault.vault_delete("MISSING")


# ---------------------------------------------------------------------------
# vault_exists
# ---------------------------------------------------------------------------

class TestVaultExists(unittest.TestCase):
    def test_exits_0_when_found(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_ok("value\n")):
            with self.assertRaises(SystemExit) as cm:
                vault.vault_exists("EXISTS_KEY")
        self.assertEqual(cm.exception.code, 0)

    def test_exits_1_when_not_found(self):
        with unittest.mock.patch.object(vault, "_run", return_value=_err("", 44)):
            with self.assertRaises(SystemExit) as cm:
                vault.vault_exists("MISSING_KEY")
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# redact_task_file
# ---------------------------------------------------------------------------

class TestRedactTaskFile(unittest.TestCase):
    def test_redacts_value_in_task(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("id: task-123\ntask: vault set APOLLO_KEY sk_real_secret\n")
            path = f.name
        vault.redact_task_file(path, "APOLLO_KEY")
        text = Path(path).read_text()
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("sk_real_secret", text)
        Path(path).unlink()

    def test_case_insensitive_match(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("task: Vault Set MY_KEY supersecret\n")
            path = f.name
        vault.redact_task_file(path, "MY_KEY")
        text = Path(path).read_text()
        self.assertNotIn("supersecret", text)
        Path(path).unlink()

    def test_no_match_leaves_file_unchanged(self):
        original = "task: do something unrelated\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(original)
            path = f.name
        vault.redact_task_file(path, "OTHER_KEY")
        self.assertEqual(Path(path).read_text(), original)
        Path(path).unlink()

    def test_missing_file_is_noop(self):
        vault.redact_task_file("/tmp/does-not-exist-sutando-vault-test.txt", "KEY")

    def test_only_redacts_matching_key(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("task: vault set APOLLO_KEY real_value\n")
            path = f.name
        vault.redact_task_file(path, "DIFFERENT_KEY")
        text = Path(path).read_text()
        self.assertIn("real_value", text)
        Path(path).unlink()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestVaultSet, TestVaultGet, TestVaultList, TestVaultDelete,
        TestVaultExists, TestRedactTaskFile,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
