#!/usr/bin/env python3
"""Tests for src/context_resume.py — transcript → cleaned recent-conversation markdown.

Run: python3 tests/context-resume.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import context_resume  # noqa: E402
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
        self.assertEqual(len(out), 100)

    def test_malformed_and_meta_lines_skipped(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("not json at all\n")
        f.write(json.dumps({"type": "summary", "summary": "meta"}) + "\n")
        f.write(json.dumps(_user("survives")) + "\n")
        f.close()
        out = extract_recent_turns(Path(f.name))
        self.assertEqual(out, "**User:** survives")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "transcript.jsonl"

    def write(self, entries):
        self.path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return self.path

    def test_separators_count_at_exact_boundary(self):
        p = self.write([_user("a"), _user("b")])
        both = "**User:** a\n\n**User:** b"
        self.assertEqual(extract_recent_turns(p, max_chars=len(both)), both)
        self.assertEqual(extract_recent_turns(p, max_chars=len(both) - 1), "**User:** b")

    def test_every_small_budget_bounds_newest_user_and_assistant(self):
        for entry, prefix in [(_user("界" * 200), "**User:** "),
                              (_assistant([{"type": "text", "text": "界" * 200}]),
                               "**Assistant:** ")]:
            p = self.write([entry])
            for budget in range(1, 80):
                with self.subTest(prefix=prefix, budget=budget):
                    out = extract_recent_turns(p, max_chars=budget)
                    self.assertLessEqual(len(out), budget)
                    if out != "…":
                        self.assertTrue(out.startswith(prefix))
                        self.assertTrue(out.endswith(" […truncated]"))
                        self.assertEqual(len(out), budget)

    def test_zero_limits_return_no_context_without_opening_file(self):
        for turns, chars in [(0, 6000), (12, 0), (0, 0)]:
            with self.subTest(turns=turns, chars=chars):
                self.assertEqual(extract_recent_turns(self.path, turns, chars), "")

    def test_negative_limits_rejected_before_reading_file(self):
        for turns, chars in [(-1, 6000), (12, -1), (-1, 0), (0, -1)]:
            with self.subTest(turns=turns, chars=chars):
                with self.assertRaisesRegex(ValueError, "limits must be nonnegative"):
                    extract_recent_turns(self.path, turns, chars)

    def test_window_counts_cleaned_turns_and_preserves_order(self):
        entries = [_user(f"older-{i}") for i in range(5000)]
        entries += [_user("newest ask"), _user("[watcher-ping]"),
                    _assistant([{"type": "tool_use", "name": "Read"}]),
                    _user("<system-reminder>noise</system-reminder>"),
                    _user("newest follow-up")]
        out = extract_recent_turns(self.write(entries), max_turns=3)
        self.assertEqual(out, "**User:** newest ask\n\n**Assistant:** [ran tools: Read]"
                             "\n\n**User:** newest follow-up")

    def test_tool_summary_obeys_rendered_budget(self):
        p = self.write([_assistant([{"type": "tool_use", "name": "T" * 3000}])])
        out = extract_recent_turns(p, max_chars=100)
        self.assertEqual(len(out), 100)
        self.assertTrue(out.startswith("**Assistant:** [ran tools: "))
        self.assertTrue(out.endswith(" […truncated]"))

    def cli(self, *flags):
        return subprocess.run([sys.executable, str(ROOT / "src/context_resume.py"),
                               str(self.path), *flags], capture_output=True, text=True,
                              timeout=10, check=False)

    def test_cli_budget_excludes_only_final_framing_newline(self):
        self.write([_user("界" * 3000)])
        r = self.cli("--chars", "100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, extract_recent_turns(self.path, max_chars=100) + "\n")
        self.assertEqual(len(r.stdout), 101)

    def test_cli_nonpositive_limits_emit_no_context(self):
        self.write([_user("last")])
        for flag in ("--turns", "--chars"):
            for limit in ("0", "-1"):
                with self.subTest(flag=flag, limit=limit):
                    r = self.cli(flag, limit)
                    self.assertEqual(r.returncode, 1)
                    self.assertEqual(r.stdout, "")
                    self.assertIn("no conversation turns" if limit == "0" else
                                  "limits must be nonnegative", r.stderr)
                    self.assertNotIn("Traceback", r.stderr)


class MessageTextEdgeTests(unittest.TestCase):
    def test_non_dict_blocks_skipped(self):
        # line 69: non-dict entries in a content list are skipped, not fatal
        p = _write([
            _assistant(["bare string block", 42, {"type": "text", "text": "kept"}]),
        ])
        out = extract_recent_turns(p)
        self.assertIn("kept", out)
        self.assertNotIn("bare string block", out)

    def test_blank_and_malformed_lines_skipped(self):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("\n")                      # blank line
        f.write("{not json}\n")            # JSONDecodeError path
        f.write(json.dumps(_user("survives")) + "\n")
        f.close()
        out = extract_recent_turns(Path(f.name))
        self.assertIn("survives", out)


class LatestTranscriptTests(unittest.TestCase):
    """Cover _latest_transcript(): happy path + no-candidates failure."""

    def _fake_projects(self, with_files):
        tmp = Path(tempfile.mkdtemp())
        repo = Path(context_resume.__file__).parent.parent
        slug = context_resume.claude_project_slug(repo)
        d = tmp / slug
        d.mkdir()
        if with_files:
            old = d / "old.jsonl"
            old.write_text(json.dumps(_user("old")) + "\n")
            new = d / "new.jsonl"
            new.write_text(json.dumps(_user("new")) + "\n")
            import os
            import time
            past = time.time() - 1000
            os.utime(old, (past, past))
        return tmp

    def test_latest_picks_newest_mtime(self):
        from unittest import mock
        proj = self._fake_projects(with_files=True)
        fake = mock.Mock()
        fake.stdout = str(proj) + "\n"
        with mock.patch.object(context_resume.subprocess, "run", return_value=fake):
            got = context_resume._latest_transcript()
        self.assertEqual(got.name, "new.jsonl")

    def test_latest_raises_when_empty(self):
        from unittest import mock
        proj = self._fake_projects(with_files=False)
        fake = mock.Mock()
        fake.stdout = str(proj) + "\n"
        with mock.patch.object(context_resume.subprocess, "run", return_value=fake):
            with self.assertRaises(FileNotFoundError):
                context_resume._latest_transcript()


class MainCliTests(unittest.TestCase):
    """Cover main(): explicit-path happy, missing file, --latest, empty output."""

    def _run_main(self, argv):
        from unittest import mock
        with mock.patch.object(sys, "argv", ["context_resume.py"] + argv):
            return context_resume.main()

    def test_main_explicit_path_ok(self):
        p = _write([_user("cli happy path")])
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self._run_main([str(p)])
        self.assertEqual(rc, 0)
        self.assertIn("cli happy path", buf.getvalue())

    def test_main_missing_file_is_rc1(self):
        rc = self._run_main(["/nonexistent/never.jsonl"])
        self.assertEqual(rc, 1)

    def test_main_no_arg_is_rc1(self):
        # Path("") → not a file → one-line loud failure
        rc = self._run_main([])
        self.assertEqual(rc, 1)

    def test_main_empty_transcript_is_rc1(self):
        p = _write([])  # zero turns → "no conversation turns found"
        rc = self._run_main([str(p)])
        self.assertEqual(rc, 1)

    def test_main_latest_flag_uses_fallback(self):
        from unittest import mock
        p = _write([_user("via latest")])
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(context_resume, "_latest_transcript", return_value=p):
            with redirect_stdout(buf):
                rc = self._run_main(["--latest"])
        self.assertEqual(rc, 0)
        self.assertIn("via latest", buf.getvalue())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
