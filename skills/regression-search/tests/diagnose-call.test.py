#!/usr/bin/env python3
"""
Tests for skills/regression-search/scripts/diagnose-call.py

Coverage:
  _truncate          — short passthrough, at-limit passthrough, over-limit
                       truncated with ellipsis, custom n, whitespace stripped
  _ts_iso            — unix 0 → epoch Z, positive unix → ISO Z, handles None→0
  analyze_turns      — empty yields zeros, refusal counted, error counted,
                       silence counted, repeated user (long) counted, short
                       repeat ignored, endings: normal/abrupt-user/sutando-silence,
                       refusal+error both captured
  find_call          — no db → None, full SID match, suffix match, no match → None,
                       multiple suffix matches → most recent, transcript reconstructed
  find_metrics       — no db → None, matching call returned with tool_calls +
                       events, no match → None
  _build_transcript  — empty session → empty string, assistant/model/agent roles →
                       Sutando prefix, tool_call rows skipped

Run: python3 skills/regression-search/tests/diagnose-call.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent

_fake_wd = type(sys)("workspace_default")
_fake_wd.resolve_workspace = lambda *a, **kw: Path(tempfile.mkdtemp())
_fake_wd.status_read_path = lambda _name: None
sys.modules["workspace_default"] = _fake_wd

_spec = importlib.util.spec_from_file_location(
    "diagnose_call",
    REPO / "skills" / "regression-search" / "scripts" / "diagnose-call.py",
)
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)

TMPDIR = Path(tempfile.mkdtemp())


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a minimal conversation.sqlite schema at path."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            ts_unix REAL, source TEXT, session_id TEXT, call_sid TEXT,
            caller TEXT, is_owner INTEGER, is_meeting INTEGER,
            duration_ms INTEGER, transcript_lines INTEGER, tool_count INTEGER,
            pending_tasks INTEGER
        );
        CREATE TABLE voice (
            ts_unix REAL, kind TEXT, text TEXT, session_id TEXT, duration_ms INTEGER
        );
        CREATE TABLE phone (
            ts_unix REAL, kind TEXT, text TEXT, session_id TEXT, duration_ms INTEGER
        );
        CREATE TABLE discord_voice (
            ts_unix REAL, kind TEXT, text TEXT, session_id TEXT, duration_ms INTEGER
        );
        CREATE TABLE session_events (
            ts_unix REAL, event_name TEXT, session_id TEXT, call_sid TEXT
        );
    """)
    conn.commit()
    return conn


# ── _truncate ─────────────────────────────────────────────────────────────────

class TestTruncate(unittest.TestCase):
    def test_short_string_passthrough(self):
        self.assertEqual(dc._truncate("hello"), "hello")

    def test_exactly_at_limit_passthrough(self):
        s = "x" * 120
        self.assertEqual(dc._truncate(s), s)

    def test_over_limit_truncated_with_ellipsis(self):
        s = "x" * 130
        result = dc._truncate(s)
        self.assertTrue(result.endswith("..."))
        self.assertEqual(len(result), 123)  # 120 + "..."

    def test_custom_n(self):
        result = dc._truncate("abcdefgh", n=5)
        self.assertTrue(result.endswith("..."))
        self.assertEqual(len(result), 8)  # 5 + "..."

    def test_strips_whitespace(self):
        self.assertEqual(dc._truncate("  hello  "), "hello")


# ── _ts_iso ───────────────────────────────────────────────────────────────────

class TestTsIso(unittest.TestCase):
    def test_zero_returns_epoch_z(self):
        result = dc._ts_iso(0)
        self.assertTrue(result.endswith("Z"))
        self.assertIn("1970", result)

    def test_positive_unix_returns_iso_z(self):
        result = dc._ts_iso(1746086400)  # 2026-01-01 ish
        self.assertTrue(result.endswith("Z"))
        self.assertIn("T", result)

    def test_none_treated_as_zero(self):
        result = dc._ts_iso(None)
        self.assertIn("1970", result)


# ── analyze_turns ─────────────────────────────────────────────────────────────

class TestAnalyzeTurns(unittest.TestCase):
    def test_empty_list_yields_zeros(self):
        r = dc.analyze_turns([])
        self.assertEqual(r["total_turns"], 0)
        self.assertEqual(r["sutando_turns"], 0)
        self.assertEqual(r["user_turns"], 0)
        self.assertEqual(r["refusals"], [])
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["silences"], 0)
        self.assertEqual(r["repeated_user"], [])
        self.assertEqual(r["ending"], "normal")

    def test_turn_counts(self):
        turns = [("user", "hi"), ("sutando", "hello"), ("user", "bye")]
        r = dc.analyze_turns(turns)
        self.assertEqual(r["total_turns"], 3)
        self.assertEqual(r["sutando_turns"], 1)
        self.assertEqual(r["user_turns"], 2)

    def test_refusal_cant(self):
        turns = [("user", "record"), ("sutando", "I can't do that")]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["refusals"]), 1)

    def test_refusal_im_not_able(self):
        turns = [("user", "play"), ("sutando", "I'm not able to do that")]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["refusals"]), 1)

    def test_refusal_unable_to(self):
        turns = [("user", "go"), ("sutando", "unable to proceed")]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["refusals"]), 1)

    def test_error_detected(self):
        turns = [("user", "try"), ("sutando", "there was an error")]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["errors"]), 1)

    def test_failed_detected(self):
        turns = [("user", "go"), ("sutando", "the request failed")]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["errors"]), 1)

    def test_silence_counted(self):
        turns = [("user", "hello?"), ("sutando", "(silence)")]
        r = dc.analyze_turns(turns)
        self.assertEqual(r["silences"], 1)

    def test_multiple_silences(self):
        turns = [
            ("user", "test"), ("sutando", "(silence)"),
            ("user", "still there?"), ("sutando", "(silence)"),
        ]
        r = dc.analyze_turns(turns)
        self.assertEqual(r["silences"], 2)

    def test_user_repeat_counted(self):
        turns = [
            ("user", "please record this now"),
            ("sutando", "one moment"),
            ("user", "please record this now"),
        ]
        r = dc.analyze_turns(turns)
        self.assertEqual(len(r["repeated_user"]), 1)

    def test_short_repeat_ignored(self):
        turns = [("user", "ok"), ("sutando", "sure"), ("user", "ok")]
        r = dc.analyze_turns(turns)
        self.assertEqual(r["repeated_user"], [])

    def test_ending_normal(self):
        turns = [("user", "goodbye"), ("sutando", "have a great day everyone!")]
        r = dc.analyze_turns(turns)
        self.assertEqual(r["ending"], "normal")

    def test_ending_abrupt_short_user(self):
        turns = [("sutando", "anything else?"), ("user", "bye")]
        r = dc.analyze_turns(turns)
        self.assertIn("abrupt", r["ending"])

    def test_ending_sutando_silence(self):
        turns = [("user", "hello"), ("sutando", "(silence)")]
        r = dc.analyze_turns(turns)
        self.assertIn("silence", r["ending"])

    def test_refusal_and_error_both_captured(self):
        turns = [
            ("user", "do the thing"),
            ("sutando", "I'm unable to help, there was an error"),
        ]
        r = dc.analyze_turns(turns)
        self.assertGreater(len(r["refusals"]), 0)
        self.assertGreater(len(r["errors"]), 0)


# ── _build_transcript ────────────────────────────────────────────────────────

class TestBuildTranscript(unittest.TestCase):
    def setUp(self):
        self.db_path = TMPDIR / "test_build.sqlite"
        self.conn = _make_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def test_empty_session_returns_empty(self):
        result = dc._build_transcript(self.conn, "sess-none")
        self.assertEqual(result, "")

    def test_no_session_id_returns_empty(self):
        result = dc._build_transcript(self.conn, "")
        self.assertEqual(result, "")

    def test_assistant_role_becomes_sutando(self):
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (1, 'assistant', 'hello', 'sess1')"
        )
        self.conn.commit()
        result = dc._build_transcript(self.conn, "sess1")
        self.assertIn("Sutando: hello", result)

    def test_user_role_becomes_user(self):
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (1, 'user', 'hi', 'sess2')"
        )
        self.conn.commit()
        result = dc._build_transcript(self.conn, "sess2")
        self.assertIn("User: hi", result)

    def test_tool_call_rows_skipped(self):
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (1, 'tool_call', 'get_weather', 'sess3')"
        )
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (2, 'assistant', 'done', 'sess3')"
        )
        self.conn.commit()
        result = dc._build_transcript(self.conn, "sess3")
        self.assertNotIn("get_weather", result)
        self.assertIn("Sutando: done", result)

    def test_rows_from_phone_table_included(self):
        self.conn.execute(
            "INSERT INTO phone (ts_unix, kind, text, session_id) VALUES (1, 'assistant', 'phone reply', 'sess4')"
        )
        self.conn.commit()
        result = dc._build_transcript(self.conn, "sess4")
        self.assertIn("Sutando: phone reply", result)


# ── find_call ─────────────────────────────────────────────────────────────────

class TestFindCall(unittest.TestCase):
    def setUp(self):
        self.db_path = TMPDIR / "test_find_call.sqlite"
        self.conn = _make_db(self.db_path)
        dc.DB_FILE = self.db_path

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def _insert_session(self, call_sid, session_id, ts_unix=1000.0):
        self.conn.execute(
            "INSERT INTO sessions (ts_unix, source, session_id, call_sid) VALUES (?, 'voice', ?, ?)",
            (ts_unix, session_id, call_sid),
        )
        self.conn.commit()

    def test_no_db_returns_none(self):
        dc.DB_FILE = TMPDIR / "nonexistent.sqlite"
        self.assertIsNone(dc.find_call("CA123"))

    def test_full_sid_match(self):
        self._insert_session("CA1234567890abcdef", "sess-1")
        result = dc.find_call("CA1234567890abcdef")
        self.assertIsNotNone(result)
        self.assertEqual(result["callSid"], "CA1234567890abcdef")

    def test_suffix_match(self):
        self._insert_session("CA1234567890abcdef", "sess-2")
        result = dc.find_call("90abcdef")
        self.assertIsNotNone(result)

    def test_no_match_returns_none(self):
        self._insert_session("CA111", "sess-3")
        self.assertIsNone(dc.find_call("ZZ999"))

    def test_multiple_suffix_matches_most_recent(self):
        self._insert_session("CAfooABCD", "sess-4", ts_unix=1000.0)
        self._insert_session("CAbarABCD", "sess-5", ts_unix=2000.0)
        result = dc.find_call("ABCD")
        self.assertEqual(result["callSid"], "CAbarABCD")

    def test_transcript_reconstructed(self):
        self._insert_session("CA-transcript", "sess-t")
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (1, 'user', 'hello', 'sess-t')"
        )
        self.conn.commit()
        result = dc.find_call("CA-transcript")
        self.assertIn("User: hello", result["transcript"])

    def test_result_has_timestamp(self):
        self._insert_session("CA-ts", "sess-ts", ts_unix=1746086400.0)
        result = dc.find_call("CA-ts")
        self.assertIn("T", result["timestamp"])
        self.assertTrue(result["timestamp"].endswith("Z"))


# ── find_metrics ──────────────────────────────────────────────────────────────

class TestFindMetrics(unittest.TestCase):
    def setUp(self):
        self.db_path = TMPDIR / "test_find_metrics.sqlite"
        self.conn = _make_db(self.db_path)
        dc.DB_FILE = self.db_path

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def _insert_session(self, call_sid, session_id="sess-m", ts_unix=1000.0, **kwargs):
        cols = "ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting, duration_ms, transcript_lines, tool_count, pending_tasks"
        vals = (
            ts_unix, "voice", session_id, call_sid,
            kwargs.get("caller", "+1555"), kwargs.get("is_owner", 1),
            kwargs.get("is_meeting", 0), kwargs.get("duration_ms", 30000),
            kwargs.get("transcript_lines", 10), kwargs.get("tool_count", 2),
            kwargs.get("pending_tasks", 0),
        )
        self.conn.execute(f"INSERT INTO sessions ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
        self.conn.commit()

    def test_no_db_returns_none(self):
        dc.DB_FILE = TMPDIR / "nonexistent2.sqlite"
        self.assertIsNone(dc.find_metrics("CA123"))

    def test_non_matching_returns_none(self):
        self._insert_session("CA111")
        self.assertIsNone(dc.find_metrics("CA999"))

    def test_matching_call_returned(self):
        self._insert_session("CA-met", session_id="sess-met", duration_ms=60000)
        result = dc.find_metrics("CA-met")
        self.assertIsNotNone(result)
        self.assertEqual(result["durationMs"], 60000)

    def test_is_owner_returned(self):
        self._insert_session("CA-own", session_id="sess-own", is_owner=1)
        result = dc.find_metrics("CA-own")
        self.assertTrue(result["isOwner"])

    def test_tool_calls_included(self):
        self._insert_session("CA-tools", session_id="sess-tools")
        self.conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id, duration_ms) VALUES (1, 'tool_call', 'get_weather', 'sess-tools', 500)"
        )
        self.conn.commit()
        result = dc.find_metrics("CA-tools")
        self.assertEqual(len(result["toolCalls"]), 1)
        self.assertEqual(result["toolCalls"][0]["name"], "get_weather")

    def test_events_included(self):
        self._insert_session("CA-evts", session_id="sess-evts")
        self.conn.execute(
            "INSERT INTO session_events (ts_unix, event_name, session_id, call_sid) VALUES (1, 'call_started', 'sess-evts', 'CA-evts')"
        )
        self.conn.commit()
        result = dc.find_metrics("CA-evts")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["event"], "call_started")


if __name__ == "__main__":
    unittest.main()
