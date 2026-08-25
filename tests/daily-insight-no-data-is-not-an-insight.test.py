#!/usr/bin/env python3
""""Not enough data" must not be delivered AS an insight.

Regression test for an owner-visible falsehood in the 2026-08-25 briefing:

    Insight: Not enough data yet to generate behavioral insights.

`generate_insight()` returned that prose when it had nothing to say, so every
consumer downstream faithfully treated it as a finding: `main()` wrote it to
`results/insight-<date>.txt` (which a bridge delivers) and stamped it into the
sentinel, and `morning-briefing.get_daily_insight()` read it back out of that
sentinel and spoke it.

morning-briefing already guards the *shape* of an insight — it skips strings
containing `{` or more than two colons, and anything under 20 characters. A
prose placeholder passes all three, which is why the guard did not catch this.
The condition is "the generator had nothing", and only the generator can say so.

Test 2 is the control: a real insight must still be returned and still be
written, so the fix cannot pass by suppressing everything.

Run: python3 tests/daily-insight-no-data-is-not-an-insight.test.py
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_insight", REPO / "src" / "daily-insight.py")
di = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(di)

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'  ok  ' if ok else 'FAIL  '}{name}{'' if ok else f' — {detail}'}")
    if not ok:
        failures.append(name)


# 1. Nothing to say → None, not prose.
di_orig = (di.analyze_dev_activity, di.load_calls, di.analyze_task_patterns, di.analyze_note_activity)
di.analyze_dev_activity = lambda *a, **k: {}
di.dev_activity_insight = lambda *a, **k: None
di.load_calls = lambda *a, **k: []
di.analyze_task_patterns = lambda *a, **k: None
di.analyze_note_activity = lambda *a, **k: {"age_known": False, "recent_7d": 0, "total": 0, "top_tags": []}
got = di.generate_insight()
check("no data yields None, not placeholder prose", got is None, f"got {got!r}")

# 1b. The exact string that reached the owner must not be produced at all.
check(
    "the placeholder string is never returned",
    got is None or "Not enough data" not in got,
    f"got {got!r}",
)

# 2. CONTROL — a real insight is still produced. Without this, returning None
#    unconditionally would pass test 1.
di.analyze_dev_activity = di_orig[0]
di.dev_activity_insight_orig = di.dev_activity_insight
di.dev_activity_insight = lambda *a, **k: "Shipped 3 PRs and 210 lines of tests today."
real = di.generate_insight()
check("a real insight is still returned", real is not None and "Shipped 3 PRs" in str(real), f"got {real!r}")
di.dev_activity_insight = di.dev_activity_insight_orig

# 3. main() with no insight: sentinel stamped EMPTY (dedupe preserved), and NO
#    results file — so nothing is delivered.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "results").mkdir()
    (tmp / "state").mkdir()
    di.RESULTS_DIR, di.STATE_DIR = tmp / "results", tmp / "state"
    di.analyze_dev_activity = lambda *a, **k: {}
    di.dev_activity_insight = lambda *a, **k: None
    di.analyze_note_activity = lambda *a, **k: {"age_known": False, "recent_7d": 0, "total": 0, "top_tags": []}
    di.main()
    sentinels = list((tmp / "state").glob("daily-insight-*.sentinel"))
    results = list((tmp / "results").glob("insight-*.txt"))
    check("sentinel is stamped (same-day dedupe preserved)", len(sentinels) == 1, f"{sentinels}")
    check("sentinel is EMPTY", sentinels and sentinels[0].read_text().strip() == "",
          f"{sentinels[0].read_text()!r}" if sentinels else "no sentinel")
    check("no results file written, so nothing is delivered", results == [], f"{results}")

    # 4. Round trip through the briefing's own reader: an empty sentinel must
    #    yield no insight, which is what removes the spoken line.
    body = sentinels[0].read_text().strip() if sentinels else ""
    check("briefing reader contract (`.strip() or None`) yields None", (body or None) is None)

di.analyze_dev_activity, di.load_calls, di.analyze_task_patterns, di.analyze_note_activity = di_orig
print("\nFAILED" if failures else "\nAll checks passed")
raise SystemExit(1 if failures else 0)
