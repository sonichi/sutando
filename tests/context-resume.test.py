#!/usr/bin/env python3
"""Tests for src/context_resume.py — transcript → cleaned recent-conversation markdown.

Run: python3 tests/context-resume.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from context_resume import extract_recent_turns  # noqa: E402


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _write(entries):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, encoding="utf-8")
    for e in entries:
        f.write(json.dumps(e) + "\n")
    f.close()
    return Path(f.name)


class ExtractTests(unittest.TestCase):
    def test_basic_turns_in_order(self):
        p = _write([
            _user("first question"),
            _assistant([{"type": "text", "text": "first answer"}]),
            _user("second question"),
        ])
        out = extract_recent_turns(p)
        self.assertIn("**User:** first question", out)
        self.assertIn("**Assistant:** first answer", out)
        self.assertLess(out.index("first question"), out.index("second question"))

    def test_max_turns_keeps_newest(self):
        p = _write([_user(f"msg {i}") for i in range(20)])
        out = extract_recent_turns(p, max_turns=3)
        self.assertNotIn("msg 16", out)
        for i in (17, 18, 19):
            self.assertIn(f"msg {i}", out)

    def test_system_noise_stripped(self):
        p = _write([
            _user("<system-reminder>secret harness stuff</system-reminder>real ask"),
            _user("[watcher-ping]"),
            _user("Caveat: The messages below were generated while running local commands."),
        ])
        out = extract_recent_turns(p)
        self.assertIn("real ask", out)
        self.assertNotIn("secret harness stuff", out)
        self.assertNotIn("watcher-ping", out)
        self.assertNotIn("Caveat:", out)

    def test_tool_only_assistant_summarized(self):
        p = _write([
            _assistant([{"type": "tool_use", "name": "Bash", "input": {}},
                        {"type": "tool_use", "name": "Read", "input": {}}]),
        ])
        out = extract_recent_turns(p)
        self.assertIn("[ran tools: Bash, Read]", out)

    def test_tool_result_user_entries_skipped(self):
        # tool_result echoes arrive as user-type entries with block content
        p = _write([
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "raw output"}]}},
            _user("actual human message"),
        ])
        out = extract_recent_turns(p)
        self.assertIn("actual human message", out)
        self.assertNotIn("raw output", out)

    def test_char_budget_keeps_newest(self):
        p = _write([_user("A" * 500), _user("B" * 500), _user("C" * 500)])
        out = extract_recent_turns(p, max_chars=600)
        self.assertIn("C" * 500, out)
        self.assertNotIn("A" * 500, out)

    def test_single_message_exceeding_budget_still_renders(self):
        p = _write([_user("X" * 3000)])
        out = extract_recent_turns(p, max_chars=100)
        self.assertTrue(out.startswith("**User:**"))
        self.assertIn("[…truncated]", out)

    def test_malformed_and_meta_lines_skipped(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("not json at all\n")
        f.write(json.dumps({"type": "summary", "summary": "meta"}) + "\n")
        f.write(json.dumps(_user("survives")) + "\n")
        f.close()
        out = extract_recent_turns(Path(f.name))
        self.assertEqual(out, "**User:** survives")


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
