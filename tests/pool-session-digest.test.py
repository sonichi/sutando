#!/usr/bin/env python3
"""Contract for scripts/pool-session-digest.py.

The age column is the load-bearing part: an operator reads it to decide whether
a session is wedged. Transcripts stamp UTC, so reading them with a local-time
parser reports an age that is wrong by the UTC offset -- and correcting with
time.timezone rather than the DST-aware offset lands exactly one hour out, which
is precisely the shape of "stale" an operator is looking for. The first version
of this script shipped that bug and reported 3-second-old activity as 60m.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest.mock
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pool-session-digest.py"

_spec = importlib.util.spec_from_file_location("digest_mod", SCRIPT)
digest_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest_mod)


def utc_iso(offset_secs: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(time.time() - offset_secs))


class AgeTest(unittest.TestCase):
    def test_recent_utc_stamp_reads_as_seconds_not_hours(self):
        """A local-time parse of a UTC stamp is off by the whole UTC offset."""
        self.assertTrue(digest_mod.age(utc_iso(3)).endswith("s ago"),
                        "a 3s-old event must not read as minutes or hours")

    def test_age_scales(self):
        self.assertTrue(digest_mod.age(utc_iso(600)).endswith("m ago"))
        self.assertTrue(digest_mod.age(utc_iso(3 * 3600)).endswith("h ago"))

    def test_age_is_never_negative(self):
        """Clock skew must not render a future stamp as a huge negative age."""
        self.assertEqual(digest_mod.age(utc_iso(-120)), "0s ago")

    def test_unparsable_stamp_is_reported_not_raised(self):
        self.assertEqual(digest_mod.age("not-a-timestamp"), "?")
        self.assertEqual(digest_mod.age(""), "?")


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.jsonl"

    def _write(self, records):
        self.path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def _rec(self, blocks):
        return {"type": "assistant", "timestamp": utc_iso(1),
                "message": {"content": blocks}}

    def test_counts_blocks_and_tolerates_junk_lines(self):
        self.path.write_text(
            json.dumps(self._rec([{"type": "text", "text": "hello"}])) + "\n"
            + "{ not json\n\n"
            + json.dumps(self._rec([{"type": "thinking", "thinking": "hmm"}])) + "\n")
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=True)
        self.assertEqual(d["records"], 2, "a malformed line must not abort the scan")
        self.assertEqual(d["blocks"]["text"], 1)
        self.assertEqual(d["blocks"]["thinking"], 1)

    def test_thinking_is_counted_but_hidden_unless_requested(self):
        self._write([self._rec([{"type": "thinking", "thinking": "private"}])])
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=False)
        self.assertEqual(d["blocks"]["thinking"], 1, "still counted")
        self.assertEqual(d["tail"], [], "reasoning must not leak without --thinking")

    def test_tail_is_bounded_to_keep(self):
        self._write([self._rec([{"type": "text", "text": f"m{i}"}]) for i in range(50)])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(len(d["tail"]), 5, "tail must stay bounded on huge transcripts")
        self.assertEqual(d["tail"][-1][2], "m49", "must keep the NEWEST events")

    def test_tool_use_summarises_the_command(self):
        self._write([self._rec([{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "git status"}}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        _, kind, body = d["tail"][0]
        self.assertEqual(kind, "BASH")
        self.assertEqual(body, "git status")

    def test_long_summary_is_truncated_to_width(self):
        self._write([self._rec([{"type": "text", "text": "x" * 500}])])
        d = digest_mod.digest(self.path, keep=5, width=40, want_thinking=False)
        self.assertLessEqual(len(d["tail"][0][2]), 41)

    def test_tool_use_without_a_known_field_falls_back_to_its_input(self):
        """Not every tool names its argument command/file_path/pattern; those
        must still show something rather than an empty line."""
        self._write([self._rec([{"type": "tool_use", "name": "Odd",
                                 "input": {"unexpected": "value"}}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertIn("unexpected", d["tail"][0][2])

    def test_unknown_block_types_are_counted_but_not_shown(self):
        self._write([self._rec([{"type": "image", "source": "x"}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(d["blocks"]["image"], 1)
        self.assertEqual(d["tail"], [], "an unrenderable block must not emit a blank row")

    def test_malformed_records_are_skipped_without_raising(self):
        """Content that is not a list, blocks that are not dicts, and records
        with no timestamp all appear in real transcripts."""
        self._write([
            {"type": "user", "message": {"content": "a bare string"}},
            {"type": "assistant", "message": {"content": ["not-a-dict"]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "kept"}]}},
        ])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(d["records"], 3)
        self.assertEqual([t[2] for t in d["tail"]], ["kept"])
        self.assertEqual(d["last_ts"], "", "no record carried a timestamp")


class DiscoveryTest(unittest.TestCase):
    """The I/O layer. Every failure path here returns empty rather than raising,
    which is right for an ops script but means a broken CLI looks identical to
    an idle machine -- so each path is pinned deliberately."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name)

    def test_config_dir_prefers_env_over_default(self):
        with unittest.mock.patch.dict(os.environ, {digest_mod.CFG_ENV: str(self.cfg)}):
            self.assertEqual(digest_mod.config_dir(), self.cfg)
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(digest_mod.config_dir(), digest_mod.DEFAULT_CFG)

    def _run_result(self, rc=0, stdout="[]"):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_live_sessions_parses_success(self):
        payload = '[{"name": "core-1", "sessionId": "abc", "status": "busy", "pid": 1}]'
        with unittest.mock.patch.object(subprocess, "run",
                                        return_value=self._run_result(stdout=payload)):
            self.assertEqual(digest_mod.live_sessions()[0]["name"], "core-1")

    def test_live_sessions_empty_on_nonzero_exit(self):
        with unittest.mock.patch.object(subprocess, "run",
                                        return_value=self._run_result(rc=1, stdout="")):
            self.assertEqual(digest_mod.live_sessions(), [])

    def test_live_sessions_empty_on_unparsable_output(self):
        with unittest.mock.patch.object(subprocess, "run",
                                        return_value=self._run_result(stdout="not json")):
            self.assertEqual(digest_mod.live_sessions(), [])

    def test_live_sessions_empty_when_cli_missing_or_hangs(self):
        for exc in (FileNotFoundError("claude"), subprocess.TimeoutExpired("claude", 30)):
            with unittest.mock.patch.object(subprocess, "run", side_effect=exc):
                self.assertEqual(digest_mod.live_sessions(), [],
                                 f"{type(exc).__name__} must not propagate")

    def test_find_transcript_locates_by_session_id_else_none(self):
        proj = self.cfg / "projects" / "-some-slug"
        proj.mkdir(parents=True)
        (proj / "sid-1.jsonl").write_text("")
        with unittest.mock.patch.dict(os.environ, {digest_mod.CFG_ENV: str(self.cfg)}):
            self.assertEqual(digest_mod.find_transcript("sid-1").name, "sid-1.jsonl")
            self.assertIsNone(digest_mod.find_transcript("absent"))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name)
        self.proj = self.cfg / "projects" / "-slug"
        self.proj.mkdir(parents=True)

    def _session(self, name="core-1", sid="sid-1"):
        return {"name": name, "sessionId": sid, "status": "busy", "pid": 42}

    def _transcript(self, sid="sid-1"):
        rec = {"type": "assistant", "timestamp": utc_iso(1),
               "message": {"content": [{"type": "text", "text": "did a thing"}]}}
        (self.proj / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")

    def _main(self, argv, sessions):
        out = io.StringIO()
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["prog", *argv]), \
             unittest.mock.patch.object(digest_mod, "live_sessions", return_value=sessions), \
             unittest.mock.patch.dict(os.environ, {digest_mod.CFG_ENV: str(self.cfg)}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = digest_mod.main()
        return rc, out.getvalue(), err.getvalue()

    def test_reports_a_session_and_its_last_event(self):
        self._transcript()
        rc, out, _ = self._main([], [self._session()])
        self.assertEqual(rc, 0)
        self.assertIn("core-1", out)
        self.assertIn("did a thing", out)

    def test_no_live_sessions_is_a_nonzero_exit(self):
        rc, _, err = self._main([], [])
        self.assertEqual(rc, 1)
        self.assertIn("no live sessions", err)

    def test_session_filter_matches_and_misses(self):
        self._transcript()
        rc, out, _ = self._main(["-s", "CORE-1"], [self._session()])
        self.assertEqual(rc, 0, "filter must be case-insensitive")
        self.assertIn("core-1", out)
        rc, _, err = self._main(["-s", "nope"], [self._session()])
        self.assertEqual(rc, 1)
        self.assertIn("no session matching", err)

    def test_missing_transcript_is_reported_not_fatal(self):
        rc, out, _ = self._main([], [self._session(sid="absent")])
        self.assertEqual(rc, 0, "one session without a transcript must not abort the rest")
        self.assertIn("no transcript", out)


class TerminalSafety(unittest.TestCase):
    """This digest prints to a TTY, so transcript content must not carry controls."""

    def test_osc52_clipboard_escape_is_neutralized(self):
        out = digest_mod._one_line("safe\x1b]52;c;YXR0YWNr\x07text", 200)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("safe", out)

    def test_csi_colour_sequence_is_neutralized(self):
        self.assertNotIn("\x1b", digest_mod._one_line("a\x1b[31mred\x1b[0m", 50))

    def test_c1_and_del_are_neutralized(self):
        for ch in ("\x7f", "\x9b", "\x00"):
            with self.subTest(ch=ch):
                self.assertNotIn(ch, digest_mod._one_line(f"x{ch}y", 50))

    def test_ordinary_text_is_untouched(self):
        """Positive control: the guard must not mangle normal content."""
        self.assertEqual(digest_mod._one_line("hello  world", 50), "hello world")

    def test_tool_name_is_sanitized_too(self):
        ev = digest_mod._event({"timestamp": "2026-08-23T00:00:00Z"},
                        {"type": "tool_use", "name": "a\x1bb",
                         "input": {"command": "ls"}}, 50)
        self.assertNotIn("\x1b", ev[1])


if __name__ == "__main__":
    unittest.main()
