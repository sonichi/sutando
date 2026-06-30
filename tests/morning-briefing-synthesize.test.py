#!/usr/bin/env python3
"""Tests for the synthesize() function in src/morning-briefing.py.

synthesize() composes a voice-friendly briefing from pre-fetched data
(weather, calendar events, reminders, Discord messages, pending questions,
health issues, optional insight). It is purely text composition — no I/O,
no subprocess calls. Tests patch datetime.now() to control the hour-based
greeting so results are deterministic.

Run: python3 tests/morning-briefing-synthesize.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("morning_briefing", REPO / "src" / "morning-briefing.py")
mbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mbr)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


def _synth(
    weather=None,
    events=None,
    reminders=None,
    discord_msgs=None,
    pending_qs=None,
    health_issues=None,
    insight=None,
    hour: int = 9,
) -> str:
    """Call synthesize() with a fixed hour so greeting is deterministic."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.hour = hour
    with patch.object(mbr, "datetime", mock_dt):
        return mbr.synthesize(
            weather,
            events or [],
            reminders or [],
            discord_msgs or [],
            pending_qs or [],
            health_issues or [],
            insight,
        )


# ---------------------------------------------------------------------------
# Greeting tests
# ---------------------------------------------------------------------------

def test_greeting_morning() -> list[str]:
    """Hour < 12 → 'Good morning'."""
    fails: list[str] = []
    result = _synth(hour=8)
    check("morning greeting missing", "Good morning" in result, fails)
    return fails


def test_greeting_afternoon() -> list[str]:
    """12 <= hour < 17 → 'Good afternoon'."""
    fails: list[str] = []
    result = _synth(hour=14)
    check("afternoon greeting missing", "Good afternoon" in result, fails)
    return fails


def test_greeting_evening() -> list[str]:
    """hour >= 17 → 'Good evening'."""
    fails: list[str] = []
    result = _synth(hour=19)
    check("evening greeting missing", "Good evening" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Weather tests
# ---------------------------------------------------------------------------

def test_weather_present() -> list[str]:
    """Weather string is included when provided."""
    fails: list[str] = []
    result = _synth(weather="72°F and clear, high of 80, low of 60")
    check("weather not in output", "72°F" in result, fails)
    check("weather prefix missing", "It's" in result, fails)
    return fails


def test_weather_absent() -> list[str]:
    """No weather in output when weather is None."""
    fails: list[str] = []
    result = _synth(weather=None)
    check("'It's' appeared without weather", "It's" not in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Calendar / events tests
# ---------------------------------------------------------------------------

def test_no_events_clear_calendar() -> list[str]:
    """Empty events list → 'Your calendar is clear today'."""
    fails: list[str] = []
    result = _synth(events=[])
    check("clear calendar message missing", "calendar is clear today" in result, fails)
    return fails


def test_one_event() -> list[str]:
    """Exactly one event → 'One meeting today'."""
    fails: list[str] = []
    result = _synth(events=[{"raw": "10am Standup"}])
    check("'One meeting today' not in output", "One meeting today" in result, fails)
    check("event name not in output", "10am Standup" in result, fails)
    return fails


def test_multiple_events() -> list[str]:
    """Multiple events → count + 'First up'."""
    fails: list[str] = []
    events = [{"raw": "9am Sync"}, {"raw": "2pm Review"}, {"raw": "4pm Planning"}]
    result = _synth(events=events)
    check("count not in output", "3 meetings today" in result, fails)
    check("'First up' missing", "First up" in result, fails)
    check("first event name missing", "9am Sync" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Reminders tests
# ---------------------------------------------------------------------------

def test_reminders_listed() -> list[str]:
    """Reminders are joined into 'Reminders due:' line."""
    fails: list[str] = []
    result = _synth(reminders=["Buy groceries", "Call dentist"])
    check("Reminders due: missing", "Reminders due:" in result, fails)
    check("reminder 1 missing", "Buy groceries" in result, fails)
    check("reminder 2 missing", "Call dentist" in result, fails)
    return fails


def test_reminders_capped_at_three() -> list[str]:
    """Only first 3 reminders are included even if more are passed."""
    fails: list[str] = []
    result = _synth(reminders=["R1", "R2", "R3", "R4", "R5"])
    check("R4 leaked past cap", "R4" not in result, fails)
    check("R3 missing (within cap)", "R3" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Pending questions tests
# ---------------------------------------------------------------------------

def test_one_pending_question() -> list[str]:
    """One pending question → 'One pending question waiting'."""
    fails: list[str] = []
    result = _synth(pending_qs=["What should the retention be?"])
    check("'One pending question' missing", "One pending question" in result, fails)
    check("question text missing", "What should the retention be?" in result, fails)
    return fails


def test_multiple_pending_questions() -> list[str]:
    """Two pending questions → count + 'Top item'."""
    fails: list[str] = []
    result = _synth(pending_qs=["First question", "Second question"])
    check("count missing", "2 pending questions" in result, fails)
    check("'Top item' missing", "Top item" in result, fails)
    check("first question missing", "First question" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Discord messages tests
# ---------------------------------------------------------------------------

def test_discord_single() -> list[str]:
    """One Discord message → singular 'Discord message'."""
    fails: list[str] = []
    result = _synth(discord_msgs=["chi: hey"])
    check("Discord message count missing", "1 Discord message" in result, fails)
    check("plural wrongly used", "messages" not in result, fails)
    return fails


def test_discord_multiple() -> list[str]:
    """Multiple Discord messages → plural 'Discord messages'."""
    fails: list[str] = []
    result = _synth(discord_msgs=["chi: hey", "chi: also this", "bassil: sup"])
    check("'3 Discord messages' missing", "3 Discord messages" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Health issues tests
# ---------------------------------------------------------------------------

def test_health_issues_included() -> list[str]:
    """Health issues appear under 'System note:'."""
    fails: list[str] = []
    result = _synth(health_issues=["voice-agent: port not open"])
    check("'System note:' missing", "System note:" in result, fails)
    check("issue text missing", "voice-agent" in result, fails)
    return fails


def test_health_issues_capped_at_two() -> list[str]:
    """Only first 2 health issues are included."""
    fails: list[str] = []
    result = _synth(health_issues=["svc-a: down", "svc-b: down", "svc-c: down"])
    check("svc-c leaked past cap", "svc-c" not in result, fails)
    check("svc-a missing (within cap)", "svc-a" in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Daily insight tests
# ---------------------------------------------------------------------------

def test_insight_included() -> list[str]:
    """Clean insight string is rendered as 'Insight: <first sentence>'."""
    fails: list[str] = []
    result = _synth(insight="Users respond better when greeted by name. Other stuff follows.")
    check("'Insight:' prefix missing", "Insight:" in result, fails)
    check("first sentence missing", "Users respond better" in result, fails)
    return fails


def test_insight_with_raw_data_skipped() -> list[str]:
    """Insight containing '{' or multiple ':' (raw data) is not rendered."""
    fails: list[str] = []
    raw_insight = '{"users": 12, "rate": 0.4}: engagement data'
    result = _synth(insight=raw_insight)
    check("raw-data insight wrongly included", "Insight:" not in result, fails)
    return fails


def test_insight_too_short_skipped() -> list[str]:
    """Insight whose first sentence is <= 20 chars is skipped."""
    fails: list[str] = []
    result = _synth(insight="Short. That's all.")
    check("too-short insight wrongly included", "Insight:" not in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Clean-slate closing line
# ---------------------------------------------------------------------------

def test_clean_slate_closing() -> list[str]:
    """With no events, reminders, questions, or health issues → closing line."""
    fails: list[str] = []
    result = _synth(weather="70°F and clear, high of 75, low of 60")
    check("clean-slate closing missing", "Good day for deep work" in result, fails)
    return fails


def test_clean_slate_absent_when_busy() -> list[str]:
    """Closing line NOT added when there are pending items."""
    fails: list[str] = []
    result = _synth(
        events=[{"raw": "10am Meeting"}],
        reminders=["Call dentist"],
    )
    check("closing appeared despite pending items", "Good day for deep work" not in result, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("greeting: morning (hour < 12)", test_greeting_morning),
        ("greeting: afternoon (12-16)", test_greeting_afternoon),
        ("greeting: evening (17+)", test_greeting_evening),
        ("weather: present → 'It's ...'", test_weather_present),
        ("weather: absent → no weather line", test_weather_absent),
        ("events: none → 'calendar is clear'", test_no_events_clear_calendar),
        ("events: one → 'One meeting today'", test_one_event),
        ("events: multiple → count + 'First up'", test_multiple_events),
        ("reminders: listed under 'Reminders due'", test_reminders_listed),
        ("reminders: capped at 3", test_reminders_capped_at_three),
        ("pending questions: one → singular form", test_one_pending_question),
        ("pending questions: multiple → count + Top item", test_multiple_pending_questions),
        ("discord: singular message", test_discord_single),
        ("discord: plural messages", test_discord_multiple),
        ("health issues: 'System note:' prefix", test_health_issues_included),
        ("health issues: capped at 2", test_health_issues_capped_at_two),
        ("insight: included as first sentence", test_insight_included),
        ("insight: raw data skipped", test_insight_with_raw_data_skipped),
        ("insight: too-short first sentence skipped", test_insight_too_short_skipped),
        ("clean slate: closing line present", test_clean_slate_closing),
        ("clean slate: closing absent when busy", test_clean_slate_absent_when_busy),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nmorning-briefing synthesize: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
