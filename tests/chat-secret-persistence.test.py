#!/usr/bin/env python3
"""Inbound chat secrets are redacted before task/state/log persistence."""

import os
import re
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import chat_secret_filter
from chat_secret_filter import filter_chat_secrets, secret_handling_instruction

try:
    import secret_scanner as _secret_scanner
    HAS_DETECT_SECRETS = _secret_scanner.DETECT_SECRETS_ACTIVE
except Exception:
    HAS_DETECT_SECRETS = False


class TestChatSecretFilter(unittest.TestCase):
    def test_embedded_github_token_is_fully_redacted(self):
        token = "gh" + "p_" + "a" * 36
        result = filter_chat_secrets(f"please use {token} for the integration")
        self.assertTrue(result.detected)
        self.assertNotIn(token, result.text)
        self.assertIn("[REDACTED-GitHub Token]", result.text)

    def test_multiple_detected_secrets_on_one_line_are_redacted(self):
        github = "ghp_" + "c" * 36
        sendgrid = "SG." + "a" * 22 + "." + "b" * 43
        result = filter_chat_secrets(f"keys {github} and {sendgrid}")
        self.assertNotIn("c" * 36, result.text)
        self.assertIn("GitHub Token", result.secret_types)
        # SendGrid comes only from detect-secrets; absent it, the token stays.
        if HAS_DETECT_SECRETS:
            self.assertNotIn("b" * 43, result.text)
            self.assertIn("SendGrid API Key", result.secret_types)

    def test_multiple_fallback_secrets_on_one_line_are_redacted(self):
        github = "ghp_" + "c" * 36
        slack = "xoxb-12345678901-abcdefghij"
        result = filter_chat_secrets(f"keys {github} and {slack}")
        self.assertNotIn("c" * 36, result.text)
        self.assertNotIn(slack, result.text)
        self.assertIn("GitHub Token", result.secret_types)
        self.assertIn("Slack Token", result.secret_types)

    def test_detector_secret_not_covered_by_fallback_redacts_its_line(self):
        sendgrid = "SG." + "a" * 22 + "." + "b" * 43
        github = "ghp_" + "c" * 36

        class Hit:
            secret_type = "SendGrid API Key"
            line_number = 1
            secret_value = sendgrid

        scanner = types.SimpleNamespace(scan_and_redact=mock.Mock(return_value=([Hit()], "")))
        with mock.patch.dict(sys.modules, {"secret_scanner": scanner}):
            result = filter_chat_secrets(f"prefix {github} and {sendgrid}")
        self.assertEqual(result.text, "[REDACTED-SendGrid API Key]")

    def test_detector_hit_without_value_redacts_line_already_changed_by_fallback(self):
        github = "ghp_" + "c" * 36

        class Hit:
            secret_type = "Opaque Detector Secret"
            line_number = 1

        scanner = types.SimpleNamespace(scan_and_redact=mock.Mock(return_value=([Hit()], "")))
        with mock.patch.dict(sys.modules, {"secret_scanner": scanner}):
            result = filter_chat_secrets(f"prefix {github} suffix")
        self.assertEqual(result.text, "[REDACTED-Opaque Detector Secret]")

    def test_openai_variant_is_redacted_by_runtime_fallback(self):
        token = "sk-" + "proj-" + "x" * 48
        result = filter_chat_secrets(f"api key={token}")
        self.assertNotIn(token, result.text)
        self.assertIn("OpenAI Token", result.secret_types)

    def test_matrix_access_token_is_redacted(self):
        token = "syt_" + "matrixSyntheticValue" * 2
        result = filter_chat_secrets(f"TASKROOM_MATRIX_TOKEN={token}")
        self.assertNotIn(token, result.text)
        self.assertIn("Matrix Access Token", result.secret_types)

    def test_combined_remote_task_token_is_redacted(self):
        token = "https://gateway.example.test|" + "remoteSyntheticSecret" * 2
        result = filter_chat_secrets(f"REMOTE_TASK_TOKEN={token}")
        self.assertNotIn(token, result.text)
        self.assertIn("Remote Task Token", result.secret_types)

    def test_google_api_key_is_redacted(self):
        # This repo's own GEMINI_API_KEY has this shape. Before the pattern
        # existed the probe below returned the input unchanged with
        # secret_types=(), so a pasted model key was persisted to the task
        # file and owner-activity state.
        token = "AIza" + "S" * 35
        result = filter_chat_secrets(f"my key is {token} thanks")
        self.assertTrue(result.detected)
        self.assertNotIn(token, result.text)
        self.assertIn("Google API Key", result.secret_types)

    def test_google_api_key_redacted_end_to_end_in_persisted_text(self):
        # End-to-end persistence regression: the redacted form is what would
        # be written, so assert the raw key survives nowhere in the output.
        token = "AIza" + "0aZ_-" * 7
        persisted = filter_chat_secrets(
            f"GEMINI_API_KEY={token}\nplease configure it"
        ).text
        self.assertNotIn(token, persisted)
        self.assertNotIn("AIza" + "0aZ_-", persisted)
        self.assertIn("please configure it", persisted)

    def test_aiza_prose_is_not_flagged(self):
        # CONTROL: the 35-char tail is what makes the pattern high-precision.
        # Prose that merely contains "AIza" must NOT be redacted, or the
        # filter would be "detecting" by matching everything.
        result = filter_chat_secrets("the AIza prefix identifies Google keys")
        self.assertNotIn("Google API Key", result.secret_types)
        self.assertIn("AIza prefix", result.text)

    def test_unhandled_detector_hit_redacts_the_whole_line(self):
        class Hit:
            secret_type = "High Entropy Secret"
            line_number = 2

        fake_module = types.SimpleNamespace(
            scan_and_redact=mock.Mock(
                return_value=([Hit()], "keep\nopaque value\nkeep2")
            )
        )
        with mock.patch.dict(sys.modules, {"secret_scanner": fake_module}):
            result = filter_chat_secrets("keep\nopaque value\nkeep2")
        self.assertEqual(result.text, "keep\n[REDACTED-High Entropy Secret]\nkeep2")

    def test_short_entropy_only_hit_is_ignored(self):
        class Hit:
            secret_type = "Base64 High Entropy String"
            line_number = 1

        fake_module = types.SimpleNamespace(
            scan_and_redact=mock.Mock(
                return_value=([Hit()], "[STORED-IN-KEYCHAIN-Base64 High Entropy String]")
            )
        )
        with mock.patch.dict(sys.modules, {"secret_scanner": fake_module}):
            result = filter_chat_secrets("ordinary prose")
        self.assertEqual(result.text, "ordinary prose")
        self.assertFalse(result.detected)

    def test_detector_failure_keeps_fallback_redaction(self):
        token = "gh" + "p_" + "b" * 36
        fake_module = types.SimpleNamespace(
            scan_and_redact=mock.Mock(side_effect=RuntimeError("detector unavailable"))
        )
        with mock.patch.dict(sys.modules, {"secret_scanner": fake_module}):
            result = filter_chat_secrets(f"token={token}")
        self.assertNotIn(token, result.text)
        self.assertIn("GitHub Token", result.secret_types)

    def test_normal_prose_is_unchanged(self):
        text = "Please explain how API keys work without showing one."
        self.assertEqual(filter_chat_secrets(text).text, text)

    def test_unterminated_private_key_is_withheld_without_optional_scanner(self):
        text = "public prefix\n-----BEGIN PRIVATE KEY-----\nfixture payload\nmore payload"
        scanner = types.SimpleNamespace(scan_and_redact=mock.Mock(side_effect=RuntimeError()))
        with mock.patch.dict(sys.modules, {"secret_scanner": scanner}):
            result = filter_chat_secrets(text)
        self.assertEqual(result.text, "public prefix\n[REDACTED-Private Key]")

    def test_scanner_private_key_hit_withholds_unterminated_tail(self):
        from secret_scanner import SecretHit, redact_secrets
        text = "public prefix\n-----BEGIN PRIVATE KEY-----\nfixture payload\nmore payload"
        output = redact_secrets(text, [SecretHit("Private Key", 2)])
        self.assertEqual(output, "public prefix\n[STORED-IN-KEYCHAIN-Private Key]")

    def test_complete_private_key_preserves_following_prose(self):
        text = "public prefix\n-----BEGIN PRIVATE KEY-----\nfixture payload\n-----END PRIVATE KEY-----\npublic suffix"
        result = filter_chat_secrets(text)
        self.assertNotIn("fixture payload", result.text)
        self.assertIn("public prefix", result.text)
        self.assertIn("public suffix", result.text)

    def test_detector_after_multiline_private_key_preserves_intervening_prose(self):
        sendgrid = "SG." + "a" * 22 + "." + "b" * 43
        text = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "BKcwggSjAgEAAoIBAQC7\n"
            "-----END PRIVATE KEY-----\n"
            f"hello\n{sendgrid}"
        )
        result = filter_chat_secrets(text)
        self.assertIn("hello", result.text)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", result.text)
        self.assertNotIn("BKcwggSjAgEAAoIBAQC7", result.text)
        if HAS_DETECT_SECRETS:
            self.assertNotIn("b" * 43, result.text)

    def test_fallback_matchers_are_bounded_on_long_unmatched_runs(self):
        for unit in ("http://", "eyJ", "M"):
            text = unit * (140_000 // len(unit))
            started = time.perf_counter()
            chat_secret_filter._detector_spans(text)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 1.0, unit)

    def test_fallback_match_ranges_match_original_patterns(self):
        patterns = {
            "Remote Task Token": r"https?://(?:(?!%7[Cc])[^\s|])+(?:\||%7[Cc])[A-Za-z0-9_+/=-]{20,}",
            "JSON Web Token": r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
            "Discord Bot Token": r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{27,}",
        }
        samples = {
            "Remote Task Token": (
                "http://a.example/relay%7C" + "x" * 24 + " "
                "http://http://a.example|" + "y" * 24
            ),
            "JSON Web Token": "eyJa.b.c",
            "Discord Bot Token": "M" + "a" * 23 + "." + "b" * 6 + "." + "c" * 27,
        }
        finders = dict(chat_secret_filter._FALLBACK_PATTERNS)
        for secret_type, pattern in patterns.items():
            with self.subTest(secret_type=secret_type):
                text = samples[secret_type]
                expected = [(m.start(), m.end()) for m in re.finditer(pattern, text)]
                finder = finders[secret_type]
                actual = list(finder(text)) if callable(finder) else [
                    (m.start(), m.end()) for m in finder.finditer(text)
                ]
                self.assertEqual(actual, expected)

    def test_discord_and_slack_notice_requires_source_message_cleanup(self):
        for surface in ("Discord", "Slack"):
            notice = secret_handling_instruction(surface, ["GitHub Token"])
            self.assertIn(f"delete the original {surface} message", notice)
            self.assertIn("do not claim an unnamed redacted value was stored", notice)


class TestPersistenceWiring(unittest.TestCase):
    def test_scanner_never_creates_a_plaintext_temporary_file(self):
        source = (REPO / "src" / "secret_scanner.py").read_text()
        self.assertNotIn("NamedTemporaryFile", source)
        self.assertNotIn("scan_file(", source)
        self.assertIn("_process_line_based_plugins(", source)

    def test_slack_filters_state_task_and_adds_cleanup_notice(self):
        source = (REPO / "src" / "slack-bridge.py").read_text()
        self.assertIn("initial_secret_filter = filter_chat_secrets(text)", source)
        self.assertIn("filtered_text = filter_chat_secrets(text)", source)
        self.assertIn('secret_handling_instruction("Slack"', source)
        self.assertIn('f"{secret_notice}"', source)

    def test_discord_filters_logs_task_context_and_adds_cleanup_notice(self):
        source = (REPO / "src" / "discord-bridge.py").read_text()
        # The composition moved into src/chat_redaction.py so the reader and this
        # writer cannot drift; the log line calls the chain rather than half of it.
        self.assertIn("safe_log_text = redact_chat_body(text)", source)
        self.assertIn("from chat_redaction import redact_chat_body", source)
        self.assertIn("filtered_reply_context = filter_chat_secrets(reply_context)", source)
        self.assertIn("filtered_enriched = filter_chat_secrets(enriched)", source)
        self.assertIn('secret_handling_instruction("Discord"', source)
        self.assertIn('f"{secret_notice}"', source)

    def test_telegram_filters_logs_task_and_reply_context(self):
        source = (REPO / "src" / "telegram-bridge.py").read_text()
        self.assertIn("safe_detail_log = filter_chat_secrets(", source)
        self.assertIn("filtered_reply = filter_chat_secrets(reply_note)", source)
        self.assertIn('secret_handling_instruction(\n                    "Telegram"', source)
        self.assertIn('f"{secret_notice}"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
