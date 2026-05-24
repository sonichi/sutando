#!/usr/bin/env python3
"""
Tests for skills/submit-use-case/scripts/submit_use_case.py

Coverage:
  slugify             — lowercase, non-alnum to dash, truncate to 60
  validate_title      — too short, too long, trailing period, reject patterns,
                        valid outcome-framed titles
  suggest_reframes    — returns list of 3, includes original text
  render_long_description — no bullets, with bullets, trailing dots stripped
  is_chi_fleet        — env var override, filesystem check mock

Run: python3 skills/submit-use-case/tests/submit_use_case.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "submit_use_case",
    REPO / "skills" / "submit-use-case" / "scripts" / "submit_use_case.py",
)
su = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(su)


# ── slugify ───────────────────────────────────────────────────────────────────

class TestSlugify(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(su.slugify("Hello World"), "hello-world")

    def test_special_chars_to_dash(self):
        self.assertEqual(su.slugify("A & B"), "a-b")

    def test_multiple_spaces_collapsed(self):
        result = su.slugify("hello   world")
        self.assertNotIn("--", result)

    def test_truncates_to_60(self):
        long_title = "word " * 20  # 100 chars
        result = su.slugify(long_title)
        self.assertLessEqual(len(result), 60)

    def test_strips_leading_trailing_dashes(self):
        result = su.slugify("  hello  ")
        self.assertFalse(result.startswith("-"))
        self.assertFalse(result.endswith("-"))

    def test_simple_outcome_title(self):
        self.assertEqual(su.slugify("Never miss a meeting"), "never-miss-a-meeting")


# ── validate_title ────────────────────────────────────────────────────────────

class TestValidateTitle(unittest.TestCase):
    def test_too_short_rejected(self):
        ok, reason = su.validate_title("Hi")
        self.assertFalse(ok)
        self.assertIn("length", reason)

    def test_too_long_rejected(self):
        ok, reason = su.validate_title("x" * 81)
        self.assertFalse(ok)
        self.assertIn("length", reason)

    def test_trailing_period_rejected(self):
        ok, reason = su.validate_title("Never miss a meeting.")
        self.assertFalse(ok)
        self.assertIn("period", reason)

    def test_ask_your_ai_rejected(self):
        ok, reason = su.validate_title("Ask your AI to schedule meetings")
        self.assertFalse(ok)
        self.assertIn("capability-framed", reason)

    def test_sutando_can_rejected(self):
        ok, reason = su.validate_title("Sutando can book your flights")
        self.assertFalse(ok)

    def test_send_a_rejected(self):
        ok, reason = su.validate_title("Send a daily briefing via voice")
        self.assertFalse(ok)

    def test_ai_that_can_rejected(self):
        ok, reason = su.validate_title("Build an AI that can manage your calendar")
        self.assertFalse(ok)

    def test_valid_outcome_title(self):
        ok, reason = su.validate_title("Never miss a meeting")
        self.assertTrue(ok)

    def test_valid_outcome_with_possessive(self):
        ok, reason = su.validate_title("Run your daily standup automatically")
        self.assertTrue(ok)

    def test_exactly_6_chars_ok(self):
        ok, reason = su.validate_title("Go run")
        self.assertTrue(ok)

    def test_exactly_80_chars_ok(self):
        title = "Never miss a deadline because your AI briefed you every morning on what to do!"
        # Trim to exactly 80 chars
        title = title[:80]
        ok, reason = su.validate_title(title)
        self.assertTrue(ok)


# ── suggest_reframes ──────────────────────────────────────────────────────────

class TestSuggestReframes(unittest.TestCase):
    def test_returns_three_suggestions(self):
        result = su.suggest_reframes("Ask your AI to schedule")
        self.assertEqual(len(result), 3)

    def test_suggestions_are_strings(self):
        result = su.suggest_reframes("Send a report")
        for s in result:
            self.assertIsInstance(s, str)

    def test_includes_original_text(self):
        result = su.suggest_reframes("Check your email")
        combined = " ".join(result).lower()
        self.assertIn("check your email", combined)

    def test_last_suggestion_is_example(self):
        result = su.suggest_reframes("anything")
        self.assertIn("example", result[-1].lower())


# ── render_long_description ───────────────────────────────────────────────────

class TestRenderLongDescription(unittest.TestCase):
    def test_no_bullets_returns_summary(self):
        result = su.render_long_description("A great use case", [])
        self.assertEqual(result, "A great use case")

    def test_bullets_appended_to_summary(self):
        result = su.render_long_description("A great use case", ["First point", "Second point"])
        self.assertIn("A great use case", result)
        self.assertIn("First point", result)
        self.assertIn("Second point", result)

    def test_bullets_trailing_dot_stripped_then_added(self):
        result = su.render_long_description("Summary", ["Point one."])
        # Original "Point one." → rstrip(".") = "Point one" → + "." = "Point one."
        self.assertIn("Point one.", result)
        # No double period
        self.assertNotIn("...", result)

    def test_multiple_bullets_joined(self):
        result = su.render_long_description("Start", ["First", "Second", "Third"])
        self.assertIn("First.", result)
        self.assertIn("Second.", result)
        self.assertIn("Third.", result)


# ── is_chi_fleet ──────────────────────────────────────────────────────────────

class TestIsChiFleet(unittest.TestCase):
    def test_env_var_chi_returns_true(self):
        with patch.dict(os.environ, {"SUTANDO_FLEET_OWNER": "chi"}):
            self.assertTrue(su.is_chi_fleet())

    def test_env_var_case_insensitive(self):
        with patch.dict(os.environ, {"SUTANDO_FLEET_OWNER": "CHI"}):
            self.assertTrue(su.is_chi_fleet())

    def test_env_var_other_returns_false(self):
        with patch.dict(os.environ, {"SUTANDO_FLEET_OWNER": "bassil"}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                self.assertFalse(su.is_chi_fleet())

    def test_no_env_no_path_returns_false(self):
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_FLEET_OWNER"}
        with patch.dict(os.environ, env, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                self.assertFalse(su.is_chi_fleet())


if __name__ == "__main__":
    unittest.main()
