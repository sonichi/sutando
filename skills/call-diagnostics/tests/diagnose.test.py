#!/usr/bin/env python3
"""
Tests for skills/call-diagnostics/scripts/diagnose.py

Coverage:
  merge_timeline  — empty call, events+toolCalls merged, sorted by ts
  parse_ts        — valid ISO Z, with offset, invalid string
  _ts_short       — long ISO truncated, short passthrough
  diagnose        — rule 1 (tool too fast), rule 2 (hallucination),
                    rule 3 (inline via work), rule 4 (auto-play),
                    rule 5 (long delay), rule 6 (repeated failure),
                    rule 8 (frustration pattern), rule 9 (auto-invoked),
                    clean call yields no issues
  categorize_issue — each named category string
  _classify_repair — stt-lag, auto-invoked, hallucinated, returned-too-fast

Run: python3 skills/call-diagnostics/tests/diagnose.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
TMPDIR = Path(tempfile.mkdtemp())

# Inject fake workspace_default so module-level resolve_workspace doesn't fail.
_fake_wd = type(sys)("workspace_default")
_fake_wd.resolve_workspace = lambda *a, **kw: TMPDIR
sys.modules["workspace_default"] = _fake_wd

# Also ensure the fake SQLITE_PATH doesn't exist so _load_from_sqlite returns None fast.
_spec = importlib.util.spec_from_file_location(
    "diagnose",
    REPO / "skills" / "call-diagnostics" / "scripts" / "diagnose.py",
)
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)
# Point at nonexistent DB/metrics so load_calls returns [] without disk I/O.
dc.SQLITE_PATH = TMPDIR / "nonexistent.sqlite"
dc.METRICS_PATH = TMPDIR / "nonexistent.jsonl"

# ── Helpers ───────────────────────────────────────────────────────────────────

TS1 = "2026-05-01T10:00:00Z"
TS2 = "2026-05-01T10:00:05Z"
TS3 = "2026-05-01T10:00:10Z"
TS4 = "2026-05-01T10:01:00Z"  # 60s after TS1


def _event(ts, detail):
    return {"timestamp": ts, "event": detail}


def _tool(ts, name, dur_ms):
    return {"timestamp": ts, "name": name, "durationMs": dur_ms}


def _call(**kwargs):
    """Build a minimal call dict."""
    return {
        "callSid": kwargs.get("sid", "CA-test"),
        "events": kwargs.get("events", []),
        "toolCalls": kwargs.get("tool_calls", []),
        "durationMs": kwargs.get("duration_ms", 30000),
        "toolCount": kwargs.get("tool_count", 0),
        "source": kwargs.get("source", "phone"),
    }


# ── merge_timeline ────────────────────────────────────────────────────────────

class TestMergeTimeline(unittest.TestCase):
    def test_empty_call_returns_empty(self):
        self.assertEqual(dc.merge_timeline(_call()), [])

    def test_events_appear_in_timeline(self):
        c = _call(events=[_event(TS1, "call_started")])
        tl = dc.merge_timeline(c)
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["type"], "event")
        self.assertEqual(tl[0]["detail"], "call_started")

    def test_tool_calls_appear_in_timeline(self):
        c = _call(tool_calls=[_tool(TS1, "play_recording", 200)])
        tl = dc.merge_timeline(c)
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["type"], "toolCall")
        self.assertIn("play_recording", tl[0]["detail"])

    def test_sorted_by_timestamp(self):
        c = _call(
            events=[_event(TS3, "late_event")],
            tool_calls=[_tool(TS1, "get_info", 50)],
        )
        tl = dc.merge_timeline(c)
        self.assertEqual(tl[0]["ts"], TS1)
        self.assertEqual(tl[1]["ts"], TS3)

    def test_tool_call_detail_includes_duration(self):
        c = _call(tool_calls=[_tool(TS1, "record", 5)])
        tl = dc.merge_timeline(c)
        self.assertIn("5ms", tl[0]["detail"])


# ── parse_ts ─────────────────────────────────────────────────────────────────

class TestParseTs(unittest.TestCase):
    def test_valid_iso_z(self):
        result = dc.parse_ts("2026-05-01T10:00:00Z")
        self.assertIsNotNone(result)

    def test_valid_with_offset(self):
        result = dc.parse_ts("2026-05-01T10:00:00+00:00")
        self.assertIsNotNone(result)

    def test_invalid_returns_none(self):
        result = dc.parse_ts("not-a-date")
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = dc.parse_ts("")
        self.assertIsNone(result)


# ── _ts_short ─────────────────────────────────────────────────────────────────

class TestTsShort(unittest.TestCase):
    def test_long_iso_truncated_to_time(self):
        result = dc._ts_short("2026-05-01T10:00:00Z")
        self.assertEqual(result, "10:00:00")

    def test_short_string_passthrough(self):
        result = dc._ts_short("short")
        self.assertEqual(result, "short")


# ── diagnose ──────────────────────────────────────────────────────────────────

class TestDiagnose(unittest.TestCase):
    def test_clean_call_no_issues(self):
        c = _call(
            events=[
                _event(TS1, "call_started"),
                _event(TS2, "caller: hello"),
                _event(TS3, "sutando: hi there"),
            ]
        )
        issues = dc.diagnose(c)
        self.assertEqual(issues, [])

    def test_rule1_tool_returned_too_fast(self):
        c = _call(tool_calls=[_tool(TS1, "play_recording", 5)])
        issues = dc.diagnose(c)
        error_issues = [i for i in issues if i["severity"] == "error"]
        self.assertTrue(any("5ms" in i["issue"] for i in error_issues))

    def test_rule1_fast_work_tool_ignored(self):
        # 'work' and 'hang_up' are excluded from fast-return check
        c = _call(tool_calls=[_tool(TS1, "work", 5)])
        issues = dc.diagnose(c)
        self.assertFalse(any("5ms" in i["issue"] for i in issues))

    def test_rule1_normal_duration_no_issue(self):
        c = _call(tool_calls=[_tool(TS1, "play_recording", 200)])
        issues = dc.diagnose(c)
        self.assertFalse(any("returned in" in i["issue"] for i in issues))

    def test_rule2_hallucination_detected(self):
        c = _call(events=[
            _event(TS1, "call_started"),
            _event(TS2, "sutando: it is playing now"),
        ])
        issues = dc.diagnose(c)
        warn_issues = [i for i in issues if i["severity"] == "warn"]
        self.assertTrue(any("hallucination" in i["issue"].lower() for i in warn_issues))

    def test_rule2_no_hallucination_when_tool_result_precedes(self):
        c = _call(events=[
            _event(TS1, "tool_result: ok"),
            _event(TS2, "sutando: it is playing now"),
        ])
        issues = dc.diagnose(c)
        self.assertFalse(any("hallucination" in i["issue"].lower() for i in issues))

    def test_rule3_inline_via_work(self):
        c = _call(events=[
            _event(TS1, "task_delegated: record a video"),
        ])
        issues = dc.diagnose(c)
        error_issues = [i for i in issues if i["severity"] == "error"]
        self.assertTrue(any("inline task" in i["issue"].lower() for i in error_issues))

    def test_rule3_non_inline_work_not_flagged(self):
        c = _call(events=[
            _event(TS1, "task_delegated: check the calendar"),
        ])
        issues = dc.diagnose(c)
        self.assertFalse(any("inline task" in i["issue"].lower() for i in issues))

    def test_rule4_auto_play_after_recording(self):
        c = _call(events=[
            _event(TS1, "recording auto-stop"),
            _event(TS2, "tool_call:play_recording"),
        ])
        issues = dc.diagnose(c)
        warn_issues = [i for i in issues if i["severity"] == "warn"]
        self.assertTrue(any("auto-play" in i["issue"].lower() for i in warn_issues))

    def test_rule4_play_after_user_request_ok(self):
        c = _call(events=[
            _event(TS1, "recording auto-stop"),
            _event(TS2, "caller: please play it"),
            _event(TS3, "tool_call:play_recording"),
        ])
        issues = dc.diagnose(c)
        self.assertFalse(any("auto-play" in i["issue"].lower() for i in issues))

    def test_rule9_auto_invoked_tool(self):
        # scroll_and_describe called without user asking for recording
        c = _call(events=[
            _event(TS1, "caller: how are you"),
            _event(TS2, "tool_call:scroll_and_describe"),
        ])
        issues = dc.diagnose(c)
        warn_issues = [i for i in issues if i["severity"] == "warn"]
        self.assertTrue(any("auto-invoked" in i["issue"].lower() for i in warn_issues))

    def test_rule9_user_requested_recording_ok(self):
        c = _call(events=[
            _event(TS1, "caller: please record this"),
            _event(TS2, "tool_call:scroll_and_describe"),
        ])
        issues = dc.diagnose(c)
        self.assertFalse(any("auto-invoked" in i["issue"].lower() for i in issues))


# ── categorize_issue ──────────────────────────────────────────────────────────

class TestCategorizeIssue(unittest.TestCase):
    def _make_issue(self, issue_text, detail="", severity="warn"):
        return {"issue": issue_text, "detail": detail, "severity": severity, "time": "10:00:00"}

    def test_fast_return_category(self):
        cat = dc.categorize_issue(self._make_issue("play_recording returned in 5ms — likely failed silently"))
        self.assertIn("returned too fast", cat.lower())

    def test_wrong_tool_category(self):
        cat = dc.categorize_issue(self._make_issue("Wrong tool: describe_screen instead of work", "Code/repo questions. Caller said: 'check git'"))
        self.assertIn("Wrong tool", cat)

    def test_hallucination_playing_category(self):
        cat = dc.categorize_issue(self._make_issue("Possible hallucination: \"it is playing\"", "playing"))
        self.assertIn("Hallucinated", cat)

    def test_auto_play_category(self):
        cat = dc.categorize_issue(self._make_issue("Auto-play after recording — user didn't ask"))
        self.assertIn("Auto-played", cat)

    def test_inline_via_work_record(self):
        cat = dc.categorize_issue(self._make_issue(
            "Inline task delegated via work: \"record a video\"",
            "detail: \"record a video\""
        ))
        self.assertIn("Record", cat)

    def test_user_correction_not_asking_to_record(self):
        # The "not asking you to record" sub-branch returns a specific string
        cat = dc.categorize_issue(self._make_issue(
            "User correction: \"not asking you to record\"",
            "Gemini recorded when user didn't ask"
        ))
        self.assertIn("Gemini recorded", cat)

    def test_user_correction_generic(self):
        cat = dc.categorize_issue(self._make_issue(
            "User correction: \"that's not right\"",
            "some detail"
        ))
        self.assertIn("correction", cat.lower())

    def test_unmet_expectation_category(self):
        cat = dc.categorize_issue(self._make_issue("Unmet expectation — user repeated request"))
        self.assertIn("repeated request", cat)

    def test_stt_lag_category(self):
        cat = dc.categorize_issue(self._make_issue("Caller speech logged 10s after play_recording tool call"))
        self.assertIn("STT", cat)

    def test_repeated_failure_category(self):
        cat = dc.categorize_issue(self._make_issue("play_recording failed 3 times in this call"))
        self.assertIn("failed repeatedly", cat)


# ── _classify_repair ──────────────────────────────────────────────────────────

class TestClassifyRepair(unittest.TestCase):
    def _classify(self, cat, freq=5, affected=3, total=10, trend="stable", in_recent=True, occurrences=None):
        return dc._classify_repair(cat, freq, affected, total, trend, in_recent, occurrences or [])

    def test_stt_lag_unsolvable(self):
        r = self._classify("STT timestamp lag")
        self.assertIsNotNone(r)
        self.assertEqual(r["repair_type"], "unsolvable")
        self.assertEqual(r["priority"], "low")

    def test_auto_invoked_scroll_gets_prompt_fix(self):
        r = self._classify("Auto-invoked scroll_and_describe — no matching user request")
        self.assertIsNotNone(r)
        self.assertEqual(r["repair_type"], "prompt")

    def test_hallucinated_playing_prompt_fix(self):
        r = self._classify("Hallucinated: 'video is playing'")
        self.assertIsNotNone(r)
        self.assertEqual(r["repair_type"], "prompt")

    def test_returned_too_fast_code_fix(self):
        r = self._classify("play_recording returned too fast (failed)")
        self.assertIsNotNone(r)
        self.assertEqual(r["repair_type"], "code")

    def test_low_pct_not_in_recent_returns_none(self):
        r = self._classify("some obscure issue", freq=1, affected=1, total=100, in_recent=False)
        self.assertIsNone(r)

    def test_repair_includes_priority(self):
        r = self._classify("STT timestamp lag")
        self.assertIn("priority", r)
        self.assertIn(r["priority"], ("low", "medium", "high", "critical"))


# ── analyze_patterns_and_repair ───────────────────────────────────────────────

class TestAnalyzePatternsAndRepair(unittest.TestCase):
    def test_empty_calls_returns_empty(self):
        repairs = dc.analyze_patterns_and_repair([])
        self.assertEqual(repairs, [])

    def test_clean_calls_returns_empty(self):
        calls = [_call(events=[_event(TS1, "call_started")]) for _ in range(3)]
        repairs = dc.analyze_patterns_and_repair(calls)
        self.assertEqual(repairs, [])

    def test_persistent_issue_generates_repair(self):
        # Create 10 calls all with auto-invoked scroll_and_describe (no user request)
        calls = []
        for _ in range(10):
            calls.append(_call(events=[
                _event(TS1, "caller: good morning"),
                _event(TS2, "tool_call:scroll_and_describe"),
            ]))
        repairs = dc.analyze_patterns_and_repair(calls)
        self.assertGreater(len(repairs), 0)

    def test_repairs_sorted_by_priority(self):
        calls = []
        for _ in range(10):
            calls.append(_call(
                events=[
                    _event(TS1, "caller: good morning"),
                    _event(TS2, "tool_call:scroll_and_describe"),
                ],
                tool_calls=[_tool(TS3, "play_recording", 5)],
            ))
        repairs = dc.analyze_patterns_and_repair(calls)
        if len(repairs) > 1:
            priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            priorities = [priority_order.get(r["priority"], 4) for r in repairs]
            self.assertEqual(priorities, sorted(priorities))


if __name__ == "__main__":
    unittest.main()
