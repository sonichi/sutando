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

import vault_intercept
from vault_intercept import InterceptResult, intercept_vault_commands, redact_vault_commands


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
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(f"vault set MY_KEY {value}")
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

    def test_backtick_quoted_value(self):
        with _mock_store():
            result = intercept_vault_commands("vault set API_KEY `my-secret-token`")
        self.assertEqual(result.text, "vault set API_KEY [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["API_KEY"])

    def test_backtick_quoted_value_with_spaces(self):
        with _mock_store():
            result = intercept_vault_commands("vault set TOKEN `value with spaces`")
        self.assertEqual(result.text, "vault set TOKEN [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["TOKEN"])

    def test_backtick_value_stored_without_backticks(self):
        stored_value = []
        def _capture_run(cmd, **kw):
            if "add-generic-password" in cmd:
                w_idx = cmd.index("-w")
                stored_value.append(cmd[w_idx + 1])
            return MagicMock(returncode=0)
        with patch("vault_intercept.subprocess.run", side_effect=_capture_run), \
             patch.object(vault_intercept, "_register_key"):
            intercept_vault_commands("vault set K `secret`")
        self.assertEqual(stored_value, ["secret"])  # no backticks in stored value

    def test_empty_value_rejected(self):
        result = intercept_vault_commands('vault set FOO ""')
        self.assertIn("[VAULT-EMPTY-VALUE]", result.text)
        self.assertEqual(result.stored, [])
        self.assertIn("FOO", result.failed)

    def test_case_insensitive(self):
        with _mock_store():
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(f"VAULT SET FOO {value}")
        # Replacement normalizes to lowercase 'vault set'; secret is sanitized.
        self.assertEqual(result.text, "vault set FOO [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["FOO"])

    # --- `=` separator (2026-06-22 leak: `vault set KEY=VALUE` was uncaught) ---
    def test_equals_separator_bare(self):
        with _mock_store():
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(f"vault set MY_KEY={value}")
        self.assertEqual(result.text, "vault set MY_KEY [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["MY_KEY"])

    def test_equals_separator_with_spaces(self):
        with _mock_store():
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(f"vault set MY_KEY = {value}")
        self.assertEqual(result.text, "vault set MY_KEY [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["MY_KEY"])

    def test_equals_separator_quoted_value(self):
        with _mock_store():
            result = intercept_vault_commands('vault set API_KEY="secret value here"')
        self.assertEqual(result.text, "vault set API_KEY [STORED-IN-KEYCHAIN]")
        self.assertEqual(result.stored, ["API_KEY"])

    def test_equals_first_only_value_keeps_rest(self):
        # Only the FIRST `=` is the separator; `=` inside the value is kept.
        stored_value = []
        def _capture_run(cmd, **kw):
            if "add-generic-password" in cmd:
                stored_value.append(cmd[cmd.index("-w") + 1])
            return MagicMock(returncode=0)
        with patch("vault_intercept.subprocess.run", side_effect=_capture_run), \
             patch.object(vault_intercept, "_register_key"):
            intercept_vault_commands('vault set TOK="ab=cd=="')
        self.assertEqual(stored_value, ["ab=cd=="])

    def test_space_form_still_intercepts(self):
        # Regression: the original space-separated form is unaffected.
        with _mock_store():
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(f"vault set MY_KEY {value}")
        self.assertEqual(result.stored, ["MY_KEY"])

    def test_surrounded_by_prose(self):
        with _mock_store():
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            result = intercept_vault_commands(
                f"hey set this: vault set APOLLO_KEY {value} and use it for the integration"
            )
        self.assertIn("[STORED-IN-KEYCHAIN]", result.text)
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, ["APOLLO_KEY"])


class TestMultipleVaultSets(unittest.TestCase):
    def test_two_commands(self):
        v1 = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
        v2 = "ghp_" + "x" * 36
        msg = f"vault set KEY1 {v1}\nvault set KEY2 {v2}"
        with _mock_store():
            result = intercept_vault_commands(msg)
        self.assertNotIn(v1, result.text)
        self.assertNotIn(v2, result.text)
        self.assertEqual(sorted(result.stored), ["KEY1", "KEY2"])
        self.assertEqual(result.text.count("[STORED-IN-KEYCHAIN]"), 2)

    def test_three_commands_inline(self):
        v1 = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
        v2 = "ghp_" + "x" * 36
        v3 = "AKIA" + "B"*16  # AWS Access Key shape
        msg = f"vault set A {v1} vault set B {v2} vault set C {v3}"
        with _mock_store():
            result = intercept_vault_commands(msg)
        self.assertEqual(sorted(result.stored), ["A", "B", "C"])
        self.assertNotIn(v1, result.text)
        self.assertNotIn(v2, result.text)
        self.assertNotIn(v3, result.text)


class TestKeychainInteraction(unittest.TestCase):
    def test_calls_security_add_generic_password(self):
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            intercept_vault_commands(f"vault set MYKEY {value}")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertIn("security", args)
        self.assertIn("add-generic-password", args)
        self.assertIn("MYKEY", args)
        self.assertIn(value, args)
        self.assertIn("-U", args)   # update flag must be present

    def test_account_is_sutando(self):
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
            intercept_vault_commands(f"vault set K {value}")
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


class TestRedactVaultCommands(unittest.TestCase):
    """redact_vault_commands — scrubs vault patterns without touching Keychain."""

    def test_empty_string_unchanged(self):
        self.assertEqual(redact_vault_commands(""), "")

    def test_plain_message_unchanged(self):
        msg = "check my calendar"
        self.assertEqual(redact_vault_commands(msg), msg)

    def test_vault_set_redacted(self):
        result = redact_vault_commands("vault set SECRET mysecret")
        self.assertIn("[vault: non-owner tier — ignored]", result)
        self.assertNotIn("mysecret", result)

    def test_does_not_call_subprocess(self):
        with patch("vault_intercept.subprocess.run") as mock_run:
            redact_vault_commands("vault set K v")
        mock_run.assert_not_called()

    def test_multiple_commands_redacted(self):
        msg = "vault set A x\nvault set B y"
        result = redact_vault_commands(msg)
        self.assertEqual(result.count("[vault: non-owner tier — ignored]"), 2)
        self.assertNotIn(" x", result)
        self.assertNotIn(" y", result)

    def test_quoted_value_redacted(self):
        result = redact_vault_commands('vault set KEY "secret value"')
        self.assertNotIn("secret", result)
        self.assertIn("[vault: non-owner tier — ignored]", result)


class TestErrorHandling(unittest.TestCase):
    def test_store_failure_still_redacts(self):
        """Fail-closed: plaintext must never reach disk even when Keychain write fails."""
        value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
        failed_proc = MagicMock(returncode=1, stderr=b"boom")
        with patch("vault_intercept.subprocess.run", return_value=failed_proc):
            result = intercept_vault_commands(f"vault set K {value}")
        self.assertNotIn(value, result.text)
        self.assertIn("[VAULT-STORE-FAILED]", result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["K"])

    def test_partial_failure_redacts_all(self):
        """With N vault commands, a failure on command M must not expose 1..M-1 secrets."""
        call_count = [0]
        def _side_effect(cmd, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                return MagicMock(returncode=1, stderr=b"fail")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        v1 = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
        v2 = "ghp_" + "x" * 36
        with patch("vault_intercept.subprocess.run", side_effect=_side_effect), \
             patch.object(vault_intercept, "_register_key"):
            result = intercept_vault_commands(f"vault set A {v1}\nvault set B {v2}")
        self.assertNotIn(v1, result.text)
        self.assertNotIn(v2, result.text)
        self.assertIn("A", result.stored)
        self.assertIn("B", result.failed)

    def test_returns_namedtuple(self):
        result = intercept_vault_commands("no vault command here")
        self.assertIsInstance(result, InterceptResult)
        self.assertIsInstance(result.text, str)
        self.assertIsInstance(result.stored, list)
        self.assertIsInstance(result.failed, list)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestNoVaultCommands,
        TestSingleVaultSet,
        TestMultipleVaultSets,
        TestKeychainInteraction,
        TestRedactVaultCommands,
        TestErrorHandling,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
