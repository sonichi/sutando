#!/usr/bin/env python3
"""Use Cases card must show a real empty state, not a bare "?".

## Why this test exists

`get_score()` returns "?" when `build_log.md` is missing or has no
`**Score: …**` marker — every fresh install. The card rendered that
"?" in the 28px score style: glyph soup that reads like a bug (owner
screenshot, 2026-07-03 backlog item #2). The fix renders explanatory
empty-state copy instead, and keeps the big score when one exists.

Plain-python self-runner (no pytest in CI). Stubs the data-gathering
functions so render_dashboard() runs without subprocesses.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("dashboard", REPO / "src" / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

# Stub everything render_dashboard() gathers, so the test exercises only
# the template logic — no subprocesses, no workspace reads.
dashboard.get_health = lambda: []
dashboard.get_activity = lambda n=10: []
dashboard.get_pending_count = lambda: {"open": 0, "done": 0}
dashboard.get_system_stats = lambda: {
    "disk_free": "100GB", "battery": "—", "charging": False,
    "uptime": "00:00", "quota": {"available": True},
}
dashboard.get_outbox = lambda limit=10: []


def render_with_score(value):
    dashboard.get_score = lambda: value
    return dashboard.render_dashboard()


def test_no_score_shows_empty_state_not_question_mark():
    html = render_with_score("?")
    assert "Nothing scored yet" in html, "empty-state copy missing"
    assert '<div class="score">?</div>' not in html, 'bare "?" still rendered'


def test_real_score_still_renders_big():
    html = render_with_score("7/10")
    assert '<div class="score">7/10</div>' in html, "score display regressed"
    assert "Nothing scored yet" not in html, "empty state shown despite a score"


def main():
    failures = []
    for fn in (test_no_score_shows_empty_state_not_question_mark, test_real_score_still_renders_big):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All dashboard-usecase-empty-state tests passed.")


if __name__ == "__main__":
    main()
