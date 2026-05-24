#!/usr/bin/env python3
"""Tests for src/vault_intercept.py — bridge-level secret interception.

All Keychain writes are mocked: no real 'security' subprocess is spawned,
secrets never touch the test runner's Keychain.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from vault_intercept import InterceptResult, intercept_vault_commands


def _mock_store(monkeypatch=None):
    """Return a patcher for subprocess.run that always succeeds."""
    return patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0))


class TestNoVaultCommands(unittest.TestCase):
    def test_empty_string(self):
        result = intercept_vault_commands("")
        self.assertEqual(result.text, "")
        self.assertEqual(result.stored, [])

    def test_plain_message_unchanged(self):
        msg = "Hey, can you check my calendar?"
        result = intercept_vault_commands(msg)
        self.assertEqual(result.text, msg)
        self.assertEqual(result.stored, [])

    def test_partial_vault_word_unchanged(self):
        msg = "add to vault tomorrow"
        result = intercept_vault_commands(msg)
        self.assertEqual(result.text, msg)
        self.assertEqual(result.stored, [])


class TestSingleVaultSet(unittest.TestCase):
    def test_bare_value(self):
        with _mock_store():
            result = intercept_vault_commands("vault set MY_KEY mypassword123")
        self.assertEqual(result.text, "vault set MY_KEY [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["MY_KEY"])

    def test_double_quoted_value(self):
        with _mock_store():
            result = intercept_vault_commands('vault set API_KEY "secret value here"')
        self.assertEqual(result.text, 'vault set API_KEY [STORED-IN-KEYCHAIN]')
        self.assertEqual(result.stored, ["API_KEY"])

    def test_single_quoted_value(self):
        with _mock_store():
            result = intercept_vault_commands("vault set TOKEN 'my token value'")
        self.assertEqual(result.text, "vault set TOKEN [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["TOKEN"])

    def test_case_insensitive(self):
        with _mock_store():
            result = intercept_vault_commands("VAULT SET FOO bar")
        # Replacement normalizes to lowercase 'vault set'; secret is sanitized.
        self.assertEqual(result.text, "vault set FOO [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["FOO"])

    def test_surrounded_by_prose(self):
        with _mock_store():
            result = intercept_vault_commands(
                "hey set this: vault set APOLLO_KEY abc123 and use it for the integration"
            )
        self.assertIn("[STORED-IN-KEYCHAIN]", result.text)
        self.assertNotIn("abc123", result.text)
        self.assertEqual(result.stored, ["APOLLO_KEY"])


class TestMultipleVaultSets(unittest.TestCase):
    def test_two_commands(self):
        msg = "vault set KEY1 val1\nvault set KEY2 val2"
        with _mock_store():
            result = intercept_vault_commands(msg)
        self.assertNotIn("val1", result.text)
        self.assertNotIn("val2", result.text)
        self.assertEqual(sorted(result.stored), ["KEY1", "KEY2"])
        self.assertEqual(result.text.count("[STORED-IN-KEYCHAIN]"), 2)

    def test_three_commands_inline(self):
        msg = "vault set A x vault set B y vault set C z"
        with _mock_store():
            result = intercept_vault_commands(msg)
        self.assertEqual(sorted(result.stored), ["A", "B", "C"])
        self.assertNotIn(" x ", result.text)
        self.assertNotIn(" y ", result.text)
        self.assertNotIn(" z ", result.text)


class TestKeychainInteraction(unittest.TestCase):
    def test_calls_security_add_generic_password(self):
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            intercept_vault_commands("vault set MYKEY supersecret")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertIn("security", args)
        self.assertIn("add-generic-password", args)
        self.assertIn("MYKEY", args)
        self.assertIn("supersecret", args)
        self.assertIn("-U", args)   # update flag must be present

    def test_account_is_sutando(self):
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            intercept_vault_commands("vault set K v")
        args = mock_run.call_args[0][0]
        idx = args.index("-a")
        self.assertEqual(args[idx + 1], "sutando")

    def test_key_and_value_passed_separately(self):
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            intercept_vault_commands('vault set MY_SECRET "pa$$word"')
        args = mock_run.call_args[0][0]
        # -s KEY  and  -w VALUE  must be separate arguments (not concatenated)
        self.assertIn("-s", args)
        self.assertIn("-w", args)
        s_idx = args.index("-s")
        w_idx = args.index("-w")
        self.assertEqual(args[s_idx + 1], "MY_SECRET")
        self.assertEqual(args[w_idx + 1], "pa$$word")


class TestErrorHandling(unittest.TestCase):
    def test_keychain_failure_raises_runtime_error(self):
        failed = MagicMock(returncode=1, stderr=b"errSecDuplicateItem")
        with patch("vault_intercept.subprocess.run", return_value=failed):
            with self.assertRaises(RuntimeError) as ctx:
                intercept_vault_commands("vault set KEY val")
        self.assertIn("KEY", str(ctx.exception))

    def test_returns_namedtuple(self):
        result = intercept_vault_commands("no vault command here")
        self.assertIsInstance(result, InterceptResult)
        self.assertIsInstance(result.text, str)
        self.assertIsInstance(result.stored, list)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestNoVaultCommands,
        TestSingleVaultSet,
        TestMultipleVaultSets,
        TestKeychainInteraction,
        TestErrorHandling,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
