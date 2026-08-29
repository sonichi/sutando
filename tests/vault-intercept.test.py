#!/usr/bin/env python3
"""Tests for src/vault_intercept.py — bridge-level secret interception.

All Keychain writes are mocked: no real 'security' subprocess is spawned,
secrets never touch the test runner's Keychain. The manifest is redirected to
a temp dir for the whole module (setUpModule) — mocking `subprocess.run` stops
the Keychain half of a store, but `_store_in_keychain` calls `_register_key`
afterwards, so an unredirected run writes fake key names into the real
`<workspace>/state/secret-vault/keys.json`.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# vault_intercept imports cleanly without detect-secrets — but it FAILS CLOSED
# at runtime: with no scanner available it refuses every unquoted `vault set`
# rather than storing a value it cannot validate (see vault_intercept.py:266).
#
# That is correct production behaviour, but it means 15 of these 47 tests
# assert outcomes that only hold when scanning actually works. Without the
# package they report as ordinary failures, which reads like broken vault
# logic instead of a missing dev dependency.
#
# Skipping the whole suite (rather than the 15) is deliberate: with no scanner
# this file exercises a degraded configuration that never occurs in
# production — CI and every real deployment have detect-secrets — so a partial
# local pass would assert against a shape that does not ship.
#
# The guard does NOT apply under CI: if $CI is set the suite runs regardless,
# so a silently broken install step fails the build instead of being papered
# over as a skip.
if importlib.util.find_spec("detect_secrets") is None and not os.environ.get("CI"):
    print(
        "SKIP tests/vault-intercept.test.py — detect-secrets not installed, so\n"
        "      vault_intercept fails closed and 15 of 47 assertions cannot hold.\n"
        "      Install the test dep to run this suite:\n"
        f"          {sys.executable} -m pip install 'detect-secrets>=1.5.0'\n"
        "      (if that fails with 'externally-managed-environment' (PEP 668),\n"
        "       retry the same command with --break-system-packages)",
        file=sys.stderr,
    )
    raise SystemExit(0)

import vault_intercept
from vault_intercept import InterceptResult, intercept_vault_commands, redact_vault_commands


_manifest_tmp = None
_manifest_patches = []


def setUpModule():
    """Point the manifest at a temp dir before any test can write one.

    Redirection is module-wide, not per-test: only 14 of the ~44 store-path
    call sites here go through `_mock_store`, and a test added later that
    forgets it would write the real manifest again.
    """
    global _manifest_tmp
    _manifest_tmp = tempfile.TemporaryDirectory(prefix="vault-intercept-test-")
    fake = os.path.join(_manifest_tmp.name, "keys.json")
    _manifest_patches.extend([
        patch.object(vault_intercept, "_manifest_path", return_value=fake),
        # _read_manifest consults the legacy home-dir path directly, so a real
        # one on the runner would still be read (and re-written) without this.
        patch.object(vault_intercept, "_LEGACY_MANIFEST_PATH", fake),
    ])
    for p in _manifest_patches:
        p.start()


def tearDownModule():
    for p in reversed(_manifest_patches):
        p.stop()
    _manifest_patches.clear()
    if _manifest_tmp is not None:
        _manifest_tmp.cleanup()


def _mock_store(monkeypatch=None):
    """Return a patcher for subprocess.run that always succeeds."""
    return patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0))


class TestHermeticManifest(unittest.TestCase):
    """The suite must not register key names in the real vault manifest.

    `_mock_store` stops the `security` subprocess, which is why no secret VALUE
    leaks — but `_store_in_keychain` treats returncode 0 as success and calls
    `_register_key`, so the NAME still lands in whatever manifest resolves.
    """

    def _real_manifest_path(self):
        from workspace_default import resolve_workspace
        return os.path.join(
            str(resolve_workspace()), "state", "secret-vault", "keys.json"
        )

    def test_manifest_path_is_redirected(self):
        self.assertNotEqual(vault_intercept._manifest_path(), self._real_manifest_path())

    def test_store_writes_the_temp_manifest_and_not_the_real_one(self):
        real = self._real_manifest_path()
        before = None
        if os.path.exists(real):
            with open(real) as f:
                before = f.read()

        with _mock_store():
            vault_intercept.set_vault_key("HERMETIC_PROBE", "sk-" + "a" * 20)

        # Positive control: a redirection that silently swallowed the write
        # would pass the real-file-unchanged assertion below on its own.
        self.assertIn("HERMETIC_PROBE", vault_intercept.list_vault_keys())

        after = None
        if os.path.exists(real):
            with open(real) as f:
                after = f.read()
        self.assertEqual(before, after, f"suite wrote the real manifest at {real}")


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


class TestUnrecognizedValueFailsClosed(unittest.TestCase):
    """#2074: an unquoted value the FP guard doesn't recognize must not leak
    to disk just because scan_secrets() missed it — only genuine prose (a
    key that fullmatches a single plain lowercase word) should still pass
    through untouched.

    PR #2052 review history — two rounds of the same underlying mistake
    (enumerating "deliberate" characters instead of excluding the much
    smaller "plain prose word" set):
    - qingyun-wu round 1 (2026-07-12): only SCREAMING_SNAKE_CASE keys were
      treated as deliberate, so lowercase/camelCase/PascalCase/dash-separated
      keys still leaked (`test_*_key_variant_not_leaked` below).
    - qingyun-wu round 2 (2026-07-12): the round-1 fix's regex (`[A-Z0-9_-]`)
      didn't actually implement its own documented rule ("not a single
      all-lowercase word") — lowercase keys with OTHER punctuation
      (`apikey.vault`, `user:id`, ...) still slipped through as "prose" and
      leaked (`test_*_punctuation_key_*` below). Fixed by inverting the
      test: prose is now defined as "key fullmatches `[a-z]+`", everything
      else fails closed — a closed exclusion instead of an open-ended
      allowlist.

    All cases verified against PR head 72c2e52 with scan_secrets() stubbed
    to return [], matching real behavior when detect-secrets doesn't
    recognize the value's shape."""

    def test_discord_client_secret_shaped_value_not_leaked(self):
        # Real repro from #2074: a 32-char mixed dash/underscore client
        # secret that scan_secrets() classifies as not-a-known-secret.
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set PR_TRIAGE_ACTIVITY_SECRET {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["PR_TRIAGE_ACTIVITY_SECRET"])
        # Assert the PROPERTIES the refusal must carry, not its exact wording —
        # the previous version pinned the literal phrases "unrecognized value"
        # and "resend quoted", so any rewording broke the test without any
        # behaviour changing, which is a test that guards prose instead of
        # contract. What must never regress is that the owner is told all three
        # of: it was not stored, the value is GONE, and quoting is the fix.
        low = result.text.lower()
        self.assertIn("not stored", low, "must say it was not stored")
        self.assertTrue(
            any(w in low for w in ("discard", "gone", "not kept", "gone anywhere")),
            f"must say the VALUE IS GONE — the destructive half. Without it the "
            f"refusal reads as a harmless no-op and the owner does not know they "
            f"have to fetch the secret again. Got: {result.text!r}",
        )
        self.assertIn("quot", low, "must tell the owner that quoting is the fix")
        # NEGATIVE CONTROL — the remedy must not promise unconditional storage.
        # My first version of this message said "quoting skips this check and
        # ALWAYS STORES". Quoting does skip the classifier, but the very next
        # thing that runs is `_store_in_keychain()`, which can raise: with the
        # store failing, a QUOTED set returns `[VAULT-STORE-FAILED]`,
        # stored=[], failed=[key]. So the promise was false, in the one PR
        # whose entire purpose is not to mislead an owner about recovery — an
        # owner following it against a locked Keychain loses the fetched value
        # a SECOND time. (Caught by john-the-dev at head 4f5b27ec; he exercised
        # the branch rather than reading the sentence, which is why he found it
        # and I didn't.) This assertion exists so the promise cannot come back.
        self.assertNotIn(
            "always store", low,
            "the refusal must not promise unconditional storage: quoting skips "
            "the CLASSIFIER, not the Keychain write, and that write can fail",
        )

    def test_quoted_value_can_still_fail_to_store(self):
        """The behaviour the negative control above is about, pinned directly.

        Quoting bypasses the shape classifier — it does not guarantee the
        secret lands. If this ever starts passing `stored=[KEY]`, the recovery
        sentence's hedge ("ATTEMPTS storage") would be understating a real
        guarantee, and the message should be revisited deliberately."""
        with patch.object(vault_intercept, "_store_in_keychain",
                          side_effect=RuntimeError("keychain locked")):
            r = intercept_vault_commands(
                'vault set TELEGRAM_BOT_TOKEN "123456789:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake"')
        self.assertEqual(r.stored, [], "a failed Keychain write must not report stored")
        self.assertEqual(r.failed, ["TELEGRAM_BOT_TOKEN"])
        self.assertNotIn("123456789", r.text, "plaintext must not survive a failed store")

    def test_pa_prefixed_key_not_leaked(self):
        value = "pa-1234567890abcdefghijklmnopqrstuvwx"
        result = intercept_vault_commands(f"vault set SOME_API_KEY {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["SOME_API_KEY"])

    def test_al_prefixed_key_not_leaked(self):
        value = "al-1234567890abcdefghijklmnopqrstuvwx"
        result = intercept_vault_commands(f"vault set SOME_API_KEY {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["SOME_API_KEY"])

    def test_lowercase_snake_case_key_variant_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set pr_triage_activity_secret {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["pr_triage_activity_secret"])

    def test_pascal_case_key_variant_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set PrTriageActivitySecret {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["PrTriageActivitySecret"])

    def test_camel_case_key_variant_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set prTriageActivitySecret {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["prTriageActivitySecret"])

    def test_dash_separated_key_variant_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set SOME-KEY {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["SOME-KEY"])

    def test_lowercase_dash_separated_key_variant_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set some-key {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["some-key"])

    def test_dotted_lowercase_key_not_leaked(self):
        # qingyun-wu round 2 repro: an all-lowercase key with punctuation
        # other than dash/underscore still looked like "prose" under the
        # round-1 fix's [A-Z0-9_-] inclusion list.
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set apikey.vault {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["apikey.vault"])

    def test_slash_separated_lowercase_key_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set apikey/vault {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["apikey/vault"])

    def test_colon_separated_lowercase_key_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set user:id {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["user:id"])

    def test_plus_separated_lowercase_key_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set token+name {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["token+name"])

    def test_at_prefixed_lowercase_key_not_leaked(self):
        value = "a1b2c3d4e5f6_g7h8i9j0k1l2m3n4o5p6"
        result = intercept_vault_commands(f"vault set @token {value}")
        self.assertNotIn(value, result.text)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, ["@token"])

    def test_does_not_call_subprocess_when_unrecognized(self):
        # Fail-closed must mean "never even attempt to store" — no Keychain
        # write for a value we couldn't validate.
        with patch("vault_intercept.subprocess.run") as mock_run:
            intercept_vault_commands("vault set SOME_API_KEY pa-1234567890abcdefghijklmnopqrstuvwx")
            mock_run.assert_not_called()

    def test_prose_with_non_env_shaped_key_still_unchanged(self):
        # Regression guard for the original FP-skip behavior: ordinary
        # sentences that happen to match the loose regex syntax (lowercase,
        # non-conventional "key") must NOT be redacted just because the
        # captured value isn't a known secret.
        msg = "the vault set command works fine, thanks"
        result = intercept_vault_commands(msg)
        self.assertEqual(result.text, msg)
        self.assertEqual(result.stored, [])
        self.assertEqual(result.failed, [])

    def test_lowercase_key_bare_word_value_unchanged(self):
        msg = "vault set thing works"
        result = intercept_vault_commands(msg)
        self.assertEqual(result.text, msg)
        self.assertEqual(result.failed, [])


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

    def test_bare_value_trailing_sentence_punctuation_not_stored(self):
        # Bare sentence punctuation after an unquoted value must not become part of the stored secret.
        value = "sk-" + "a"*20 + "T3BlbkFJ" + "b"*20
        with patch("vault_intercept.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            intercept_vault_commands(f"vault set MY_KEY {value}.")
        args = mock_run.call_args[0][0]
        w_idx = args.index("-w")
        self.assertEqual(args[w_idx + 1], value)


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
    # Explicit allowlist, not unittest.main() — a new TestCase class must be
    # added here or it silently never runs (no error, just missing coverage).
    for cls in [
        TestNoVaultCommands,
        TestSingleVaultSet,
        TestUnrecognizedValueFailsClosed,
        TestMultipleVaultSets,
        TestHermeticManifest,
        TestKeychainInteraction,
        TestRedactVaultCommands,
        TestErrorHandling,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
