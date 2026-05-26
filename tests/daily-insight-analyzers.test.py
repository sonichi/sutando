#!/usr/bin/env python3
"""Behavioral tests for `src/daily-insight.py` pure analysis functions.

Functions under test — all are pure (list-in, value-out, no file I/O):

  analyze_call_timing(calls)   — (hour_counts, day_counts) Counters
  analyze_call_duration(calls) — dict of duration stats | None
  analyze_topics(calls)        — top-10 [(word, count)] from summaries

Run: python3 tests/daily-insight-analyzers.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader — daily-insight.py resolves CALLS_FILE / NOTES_DIR at
# import time; redirect workspace to a temp dir so load_calls() is a
# no-op and note scanning finds no files.
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-insight-")
os.environ["SUTANDO_WORKSPACE"] = _WS
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("daily_insight", "daily-insight", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("daily_insight", _SRC / "daily-insight.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

analyze_call_timing = _mod.analyze_call_timing
analyze_call_duration = _mod.analyze_call_duration
analyze_topics = _mod.analyze_topics

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _call(start_time: str = "", duration: int = 0, summary: str = "") -> dict:
    c: dict = {}
    if start_time:
        c["start_time"] = start_time
    if duration:
        c["duration_seconds"] = duration
    if summary:
        c["summary"] = summary
    return c


# -----------------------------------------------------------------------
# analyze_call_timing
# -----------------------------------------------------------------------

# T1 — empty list → both counters empty
hour_c, day_c = analyze_call_timing([])
if not hour_c and not day_c:
    ok("T1: analyze_call_timing([]) → both counters empty")
else:
    fail("T1: empty list", f"hour_c={dict(hour_c)} day_c={dict(day_c)}")

# T2 — single call at known hour → hour counter incremented
# 2026-05-26T14:00:00Z → hour 14 (UTC), Tuesday
hour_c, day_c = analyze_call_timing([_call("2026-05-26T14:00:00Z")])
if hour_c.get(14) == 1:
    ok("T2: single call at 14:00Z → hour 14 count = 1")
else:
    fail("T2: hour counter", f"hour_c={dict(hour_c)}")

# T3 — day-of-week counter incremented
# 2026-05-26 is a Tuesday
hour_c, day_c = analyze_call_timing([_call("2026-05-26T09:00:00Z")])
if day_c.get("Tuesday") == 1:
    ok("T3: 2026-05-26 (Tuesday) → day_counts['Tuesday'] = 1")
else:
    fail("T3: day counter", f"day_c={dict(day_c)}")

# T4 — multiple calls accumulate counts in the same hour
calls = [
    _call("2026-05-26T10:00:00Z"),
    _call("2026-05-26T10:30:00Z"),
    _call("2026-05-26T15:00:00Z"),
]
hour_c, day_c = analyze_call_timing(calls)
if hour_c.get(10) == 2 and hour_c.get(15) == 1:
    ok("T4: two calls at 10:xx, one at 15:xx → hour 10 count=2, hour 15 count=1")
else:
    fail("T4: accumulate hours", f"hour_c={dict(hour_c)}")

# T5 — malformed timestamp → skipped, no crash, counter still works for valid calls
calls = [
    {"start_time": "not-a-date"},
    _call("2026-05-26T08:00:00Z"),
]
hour_c, day_c = analyze_call_timing(calls)
if hour_c.get(8) == 1 and sum(hour_c.values()) == 1:
    ok("T5: malformed timestamp skipped, valid call still counted")
else:
    fail("T5: malformed ts skip", f"hour_c={dict(hour_c)}")

# T6 — call with no start_time field → skipped
calls = [{"duration_seconds": 120}, _call("2026-05-26T11:00:00Z")]
hour_c, day_c = analyze_call_timing(calls)
if sum(hour_c.values()) == 1:
    ok("T6: call with no start_time field skipped gracefully")
else:
    fail("T6: no start_time skip", f"hour_c total={sum(hour_c.values())}")

# T7 — +00:00 offset parsed (not just Z suffix)
hour_c, day_c = analyze_call_timing([_call("2026-05-26T16:00:00+00:00")])
if hour_c.get(16) == 1:
    ok("T7: +00:00 offset timestamp parsed (hour 16 counted)")
else:
    fail("T7: +00:00 offset", f"hour_c={dict(hour_c)}")

# -----------------------------------------------------------------------
# analyze_call_duration
# -----------------------------------------------------------------------

# T8 — empty list → None
result = analyze_call_duration([])
if result is None:
    ok("T8: analyze_call_duration([]) → None")
else:
    fail("T8: empty → None", f"got {result!r}")

# T9 — single call: count=1, avg correct, longest = avg
result = analyze_call_duration([_call(duration=120)])
if result is not None and result["count"] == 1 and result["avg_minutes"] == 2.0 and result["longest_minutes"] == 2.0:
    ok("T9: single 120s call → count=1, avg=2.0min, longest=2.0min")
else:
    fail("T9: single call stats", f"result={result}")

# T10 — avg_minutes rounds to 1 decimal
# 3 calls: 60+90+90 = 240s → avg=80s = 1.3min
result = analyze_call_duration([_call(duration=60), _call(duration=90), _call(duration=90)])
if result is not None and result["avg_minutes"] == 1.3 and result["longest_minutes"] == 1.5:
    ok("T10: avg_minutes and longest_minutes rounded to 1 decimal (240s / 3 = 80s = 1.3 min)")
else:
    fail("T10: rounding", f"result={result}")

# T11 — long_call_pct: calls >2× average are counted
# avg = 60s; a 130s call is >2×60 = long
result = analyze_call_duration([_call(duration=60), _call(duration=60), _call(duration=60), _call(duration=130)])
# avg = (60+60+60+130)/4 = 77.5s; 2*77.5 = 155; 130 < 155 → 0 long calls
# Let's use more extreme: avg around 60, one very long call
result = analyze_call_duration([_call(duration=60), _call(duration=60), _call(duration=300)])
# avg = (60+60+300)/3 = 140s; 2*140 = 280; 300 > 280 → 1 long call out of 3 → 33.3%
if result is not None and result["long_call_pct"] > 0:
    ok(f"T11: 1/3 calls >2× average → long_call_pct={result['long_call_pct']}% (>0)")
else:
    fail("T11: long_call_pct", f"result={result}")

# T12 — duration=0 skipped
result = analyze_call_duration([{"duration_seconds": 0}, _call(duration=120)])
if result is not None and result["count"] == 1:
    ok("T12: duration=0 skipped, only valid call counted (count=1)")
else:
    fail("T12: zero duration skip", f"result={result}")

# T13 — non-numeric duration skipped
result = analyze_call_duration([{"duration_seconds": "unknown"}, _call(duration=60)])
if result is not None and result["count"] == 1:
    ok("T13: non-numeric duration skipped gracefully (count=1)")
else:
    fail("T13: non-numeric duration", f"result={result}")

# T14 — all durations zero → None (no valid durations)
result = analyze_call_duration([{"duration_seconds": 0}, {"duration_seconds": 0}])
if result is None:
    ok("T14: all durations zero → None")
else:
    fail("T14: all zero → None", f"got {result!r}")

# -----------------------------------------------------------------------
# analyze_topics
# -----------------------------------------------------------------------

# T15 — empty list → empty result
result = analyze_topics([])
if result == []:
    ok("T15: analyze_topics([]) → empty list")
else:
    fail("T15: empty → []", f"got {result!r}")

# T16 — short words (≤4 chars) filtered out
result = analyze_topics([_call(summary="the cat sat and did run")])
words = [w for w, _ in result]
short_leaked = [w for w in words if len(w) <= 4]
if not short_leaked:
    ok("T16: short words (≤4 chars) filtered out of topics")
else:
    fail("T16: short word filter", f"leaked: {short_leaked}")

# T17 — common stop-words filtered out
# "about", "their", "there", "would", etc. should not appear
result = analyze_topics([_call(summary="about their ideas would there should which where")])
words = [w for w, _ in result]
stop_leaked = [w for w in words if w in {"about", "their", "there", "would", "should", "which", "where"}]
if not stop_leaked:
    ok("T17: stop-words (about/their/there/would/should/which/where) filtered out")
else:
    fail("T17: stop-word filter", f"leaked: {stop_leaked}")

# T18 — substantive words counted and returned
result = analyze_topics([
    _call(summary="machine learning pipeline"),
    _call(summary="machine learning dashboard"),
])
words = [w for w, _ in result]
if "machine" in words and "learning" in words:
    ok("T18: 'machine' and 'learning' both appear (>4 chars, not stop-words)")
else:
    fail("T18: substantive words in result", f"words={words}")

# T19 — topic counts reflect repetition frequency
result = analyze_topics([
    _call(summary="scheduling meeting scheduling meeting scheduling"),
    _call(summary="scheduling followup"),
])
# "scheduling" appears 4x, "meeting" 2x, "followup" 1x
counts = dict(result)
if counts.get("scheduling", 0) > counts.get("meeting", 0) > counts.get("followup", 0):
    ok("T19: scheduling > meeting > followup (frequency order preserved)")
else:
    fail("T19: topic frequency order", f"counts={counts}")

# T20 — at most 10 topics returned
# Create 15 distinct long-enough words
words_input = " ".join([f"keyword{i:02d}" for i in range(15)])
result = analyze_topics([_call(summary=words_input)])
if len(result) <= 10:
    ok(f"T20: analyze_topics returns at most 10 topics (got {len(result)})")
else:
    fail("T20: top-10 cap", f"returned {len(result)} topics")

# T21 — punctuation stripped from word boundaries
result = analyze_topics([_call(summary="scheduling. meeting! pipeline? dashboard,")])
words = [w for w, _ in result]
punctuated = [w for w in words if any(c in w for c in ".,!?()[]{}:;\"'")]
if not punctuated:
    ok("T21: punctuation stripped from word boundaries (no leaked punctuation in topics)")
else:
    fail("T21: punctuation strip", f"leaked punctuation in: {punctuated}")

# T22 — calls with no summary field → skipped gracefully
result = analyze_topics([{"duration_seconds": 60}, _call(summary="analysis pipeline")])
words = [w for w, _ in result]
if "analysis" in words or "pipeline" in words:
    ok("T22: call with no summary skipped; valid summary still processed")
else:
    fail("T22: missing summary field", f"words={words}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
