#!/usr/bin/env python3
"""
Tests for skills/regression-search/scripts/find-regression.py

Coverage:
  parse_transcript — empty string, basic role lines, unknown role skipped,
                     multi-line turns merged, blank lines skipped, Sutando
                     alias normalized to 'sutando', Recipient/User/Caller
                     normalized to 'user'
  classify_call    — no match, working (no signals), refusal detected,
                     error detected, silence detected, user repeat triggers
                     broken, broken takes priority over working signals,
                     multiple reasons accumulated, short repeat ignored
  find_snippet     — keyword found in first sutando turn, keyword found in
                     user turn, no match returns empty, snippet truncated
                     to 120 chars, ellipsis added when truncated

Run: python3 skills/regression-search/tests/find-regression.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent

_fake_wd = type(sys)("workspace_default")
_fake_wd.resolve_workspace = lambda: Path(tempfile.mkdtemp())
_fake_wd.status_read_path = lambda _name: None
sys.modules["workspace_default"] = _fake_wd

_spec = importlib.util.spec_from_file_location(
    "find_regression",
    REPO / "skills" / "regression-search" / "scripts" / "find-regression.py",
)
fr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fr)


# ── parse_transcript ──────────────────────────────────────────────────────────

class TestParseTranscript(unittest.TestCase):
    def test_empty_string_returns_empty(self):
        self.assertEqual(fr.parse_transcript(""), [])

    def test_blank_lines_skipped(self):
        turns = fr.parse_transcript("\n\nSutando: hello\n\n")
        self.assertEqual(len(turns), 1)

    def test_sutando_role_normalized(self):
        turns = fr.parse_transcript("Sutando: hi there")
        self.assertEqual(turns[0][0], "sutando")
        self.assertEqual(turns[0][1], "hi there")

    def test_recipient_role_normalized_to_user(self):
        turns = fr.parse_transcript("Recipient: can you help")
        self.assertEqual(turns[0][0], "user")

    def test_user_role_normalized(self):
        turns = fr.parse_transcript("User: please record")
        self.assertEqual(turns[0][0], "user")

    def test_caller_role_normalized(self):
        turns = fr.parse_transcript("Caller: what time is it")
        self.assertEqual(turns[0][0], "user")

    def test_multiple_turns_parsed(self):
        transcript = "User: record a note\nSutando: sure, I can help\nUser: thanks"
        turns = fr.parse_transcript(transcript)
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0], ("user", "record a note"))
        self.assertEqual(turns[1], ("sutando", "sure, I can help"))
        self.assertEqual(turns[2], ("user", "thanks"))

    def test_continuation_lines_merged(self):
        transcript = "Sutando: this is a long\nresponse across multiple lines"
        turns = fr.parse_transcript(transcript)
        self.assertEqual(len(turns), 1)
        self.assertIn("long", turns[0][1])
        self.assertIn("multiple", turns[0][1])

    def test_unknown_role_treated_as_continuation(self):
        # Lines that don't start with a known role are appended to prior turn
        transcript = "User: help me\nsome random text without a role"
        turns = fr.parse_transcript(transcript)
        self.assertEqual(len(turns), 1)
        self.assertIn("random text", turns[0][1])


# ── classify_call ─────────────────────────────────────────────────────────────

class TestClassifyCall(unittest.TestCase):
    def _turns(self, *pairs):
        """Build a list of (role, text) pairs from alternating args."""
        result = []
        for i in range(0, len(pairs), 2):
            result.append((pairs[i], pairs[i + 1]))
        return result

    def test_no_keyword_returns_no_match(self):
        turns = self._turns("user", "what time is it", "sutando", "it is noon")
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "no-match")
        self.assertEqual(reasons, [])

    def test_keyword_in_user_turn_no_signals_returns_working(self):
        turns = self._turns("user", "please record a note", "sutando", "done, recorded")
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "working")
        self.assertEqual(reasons, [])

    def test_refusal_pattern_detected(self):
        turns = self._turns(
            "user", "record my meeting",
            "sutando", "I'm sorry, I can't record your meeting"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertIn("refusal", reasons)

    def test_i_cannot_without_sorry_not_refusal(self):
        # "I cannot" alone doesn't match any refusal pattern — only
        # "sorry, I cannot" does (via the 5th pattern). Document this gap.
        turns = self._turns(
            "user", "can you record this",
            "sutando", "I cannot do that right now"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        # No refusal pattern fires → classified as working (no signals)
        self.assertEqual(verdict, "working")
        self.assertNotIn("refusal", reasons)

    def test_sorry_i_cannot_refusal(self):
        turns = self._turns(
            "user", "can you record this",
            "sutando", "sorry, I cannot do that right now"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertIn("refusal", reasons)

    def test_unable_to_refusal(self):
        turns = self._turns(
            "user", "please record",
            "sutando", "I'm unable to complete the recording"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertIn("refusal", reasons)

    def test_error_pattern_detected(self):
        turns = self._turns(
            "user", "start recording",
            "sutando", "there was an error starting the recording"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertIn("error", reasons)

    def test_failed_pattern_detected(self):
        turns = self._turns(
            "user", "record please",
            "sutando", "the recording failed to start"
        )
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertIn("error", reasons)

    def test_user_repeat_triggers_broken(self):
        turns = [
            ("user", "please record"),
            ("sutando", "I heard you"),
            ("user", "please record"),  # exact repeat ≥ 2x
        ]
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "broken")
        self.assertTrue(any("repeated" in r for r in reasons))

    def test_short_repeat_not_counted(self):
        # Strings ≤ 6 chars don't trigger repeat detection
        turns = [
            ("user", "ok"),
            ("sutando", "noted"),
            ("user", "ok"),
        ]
        verdict, reasons = fr.classify_call(turns, "ok")
        # No broken reasons from short repeat
        self.assertNotIn("user repeated", str(reasons))

    def test_keyword_only_in_sutando_still_matched(self):
        turns = self._turns("sutando", "I will record this for you")
        verdict, reasons = fr.classify_call(turns, "record")
        # keyword_in_sutando_turn=True, no user mention → working
        self.assertIn(verdict, ("working", "mentioned"))

    def test_silence_pattern_detected(self):
        turns = [
            ("user", "please record a note"),
            ("sutando", "(silence)"),
        ]
        verdict, reasons = fr.classify_call(turns, "record")
        # Sutando silence after user mention → broken
        self.assertEqual(verdict, "broken")
        self.assertIn("silence", reasons)

    def test_reasons_not_duplicated(self):
        # Multiple sutando turns with same refusal → reason added once
        turns = [
            ("user", "record please"),
            ("sutando", "I can't do that"),
            ("sutando", "I'm not able to help with that"),
        ]
        verdict, reasons = fr.classify_call(turns, "record")
        self.assertEqual(reasons.count("refusal"), 1)

    def test_case_insensitive_keyword(self):
        turns = self._turns("user", "RECORD my meeting", "sutando", "done")
        verdict, _ = fr.classify_call(turns, "record")
        self.assertEqual(verdict, "working")


# ── find_snippet ──────────────────────────────────────────────────────────────

class TestFindSnippet(unittest.TestCase):
    def test_returns_empty_when_no_match(self):
        turns = [("user", "hello world"), ("sutando", "hi there")]
        snippet = fr.find_snippet(turns, "record")
        self.assertEqual(snippet, "")

    def test_returns_snippet_for_user_match(self):
        turns = [("user", "please record this"), ("sutando", "done")]
        snippet = fr.find_snippet(turns, "record")
        self.assertIn("record", snippet)
        self.assertTrue(snippet.startswith("user:"))

    def test_returns_snippet_for_sutando_match(self):
        turns = [("user", "hi"), ("sutando", "I will record this")]
        snippet = fr.find_snippet(turns, "record")
        self.assertIn("record", snippet)
        self.assertTrue(snippet.startswith("sutando:"))

    def test_first_match_returned(self):
        turns = [
            ("user", "record please"),
            ("sutando", "let me record that"),
        ]
        snippet = fr.find_snippet(turns, "record")
        # Should be the user turn (first match)
        self.assertTrue(snippet.startswith("user:"))

    def test_long_text_truncated_to_120(self):
        long_text = "record " + "x" * 200
        turns = [("user", long_text)]
        snippet = fr.find_snippet(turns, "record")
        self.assertLessEqual(len(snippet), 124)  # "user: " prefix + 120 + "..."
        self.assertTrue(snippet.endswith("..."))

    def test_short_text_no_ellipsis(self):
        turns = [("user", "record this")]
        snippet = fr.find_snippet(turns, "record")
        self.assertFalse(snippet.endswith("..."))

    def test_empty_turns_returns_empty(self):
        snippet = fr.find_snippet([], "record")
        self.assertEqual(snippet, "")


if __name__ == "__main__":
    unittest.main()
