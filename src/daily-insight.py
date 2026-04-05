#!/usr/bin/env python3
"""Daily insight generator for Sutando's behavioral flywheel.

Analyzes call logs, task history, and notes to surface one actionable pattern.
Output: results/insight-{date}.txt (voice agent can speak it).
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
CALLS_FILE = WORKSPACE / "results" / "calls" / "calls.jsonl"
RESULTS_DIR = WORKSPACE / "results"
NOTES_DIR = WORKSPACE / "notes"


def load_calls():
    if not CALLS_FILE.exists():
        return []
    calls = []
    for line in CALLS_FILE.read_text().splitlines():
        if line.strip():
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return calls


def analyze_call_timing(calls):
    """Find peak usage hours and day-of-week patterns."""
    hour_counts = Counter()
    day_counts = Counter()
    for c in calls:
        ts = c.get("start_time") or c.get("timestamp") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hour_counts[dt.hour] += 1
            day_counts[dt.strftime("%A")] += 1
        except (ValueError, AttributeError):
            pass
    return hour_counts, day_counts


def analyze_call_duration(calls):
    """Find average and outlier call durations."""
    durations = []
    for c in calls:
        dur = c.get("duration_seconds") or c.get("duration")
        if dur and isinstance(dur, (int, float)) and dur > 0:
            durations.append(dur)
    if not durations:
        return None
    avg = sum(durations) / len(durations)
    long_calls = [d for d in durations if d > avg * 2]
    return {
        "count": len(durations),
        "avg_minutes": round(avg / 60, 1),
        "longest_minutes": round(max(durations) / 60, 1),
        "long_call_pct": round(len(long_calls) / len(durations) * 100, 1),
    }


def analyze_topics(calls):
    """Extract most common topics from call summaries."""
    topics = Counter()
    for c in calls:
        summary = c.get("summary", "") or c.get("topic", "") or ""
        # Simple keyword extraction
        for word in summary.lower().split():
            word = word.strip(".,!?()[]{}:;\"'")
            if len(word) > 4 and word not in {
                "about", "their", "there", "would", "could", "should",
                "which", "where", "these", "those", "other", "after",
                "before", "between", "under", "above", "through",
            }:
                topics[word] += 1
    return topics.most_common(10)


def analyze_task_patterns():
    """Look at recent task results for patterns."""
    task_files = sorted(RESULTS_DIR.glob("task-*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    sources = Counter()
    for f in task_files[:50]:
        content = f.read_text()
        if "discord" in content.lower():
            sources["Discord"] += 1
        elif "telegram" in content.lower():
            sources["Telegram"] += 1
        elif "voice" in content.lower():
            sources["Voice"] += 1
        else:
            sources["Other"] += 1
    return sources


def analyze_note_activity():
    """Check note creation patterns."""
    notes = list(NOTES_DIR.glob("*.md"))
    recent = [n for n in notes if n.stat().st_mtime > (datetime.now().timestamp() - 7 * 86400)]
    tags = Counter()
    for n in notes:
        content = n.read_text()
        if "tags:" in content:
            tag_line = [l for l in content.split("\n") if "tags:" in l]
            if tag_line:
                for tag in tag_line[0].replace("tags:", "").replace("[", "").replace("]", "").split(","):
                    tag = tag.strip()
                    if tag:
                        tags[tag] += 1
    return {"total": len(notes), "recent_7d": len(recent), "top_tags": tags.most_common(5)}


def generate_insight():
    calls = load_calls()
    insights = []

    if calls:
        hour_counts, day_counts = analyze_call_timing(calls)
        dur_stats = analyze_call_duration(calls)

        if hour_counts:
            peak_hour = hour_counts.most_common(1)[0]
            quiet_hours = [h for h in range(8, 22) if hour_counts.get(h, 0) == 0]
            if quiet_hours:
                insights.append(
                    f"You've made {len(calls)} calls total. Peak hour: {peak_hour[0]}:00 "
                    f"({peak_hour[1]} calls). Hours with zero calls: {', '.join(f'{h}:00' for h in quiet_hours[:3])}. "
                    f"Consider scheduling deep work during those quiet windows."
                )

        if day_counts:
            busiest = day_counts.most_common(1)[0]
            quietest = day_counts.most_common()[-1]
            if busiest[1] > quietest[1] * 2:
                insights.append(
                    f"{busiest[0]}s are your busiest day ({busiest[1]} calls) — "
                    f"{quietest[0]}s are quietest ({quietest[1]}). "
                    f"You might want to protect {busiest[0]} mornings for focused work."
                )

        if dur_stats and dur_stats["long_call_pct"] > 20:
            insights.append(
                f"{dur_stats['long_call_pct']}% of your calls run longer than average "
                f"({dur_stats['avg_minutes']} min avg). Longest: {dur_stats['longest_minutes']} min. "
                f"Setting a timer or agenda could reclaim significant time."
            )

    note_stats = analyze_note_activity()
    if note_stats["recent_7d"] > 5:
        insights.append(
            f"You've created {note_stats['recent_7d']} notes in the last 7 days "
            f"({note_stats['total']} total). Top tags: {', '.join(t[0] for t in note_stats['top_tags'][:3])}. "
            f"Your notes system is active and growing."
        )
    elif note_stats["recent_7d"] == 0 and note_stats["total"] > 10:
        insights.append(
            f"No new notes in the last 7 days (you have {note_stats['total']} total). "
            f"You were actively noting ideas before — might be worth capturing what you're learning this week."
        )

    task_sources = analyze_task_patterns()
    if task_sources:
        top_source = task_sources.most_common(1)[0]
        insights.append(
            f"Most tasks come through {top_source[0]} ({top_source[1]} recent). "
            f"Channel mix: {dict(task_sources)}."
        )

    if not insights:
        insights.append("Not enough data yet to generate behavioral insights. Keep using Sutando — patterns will emerge.")

    # Pick the most interesting one (longest = most specific)
    best = max(insights, key=len)
    return best


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    output_path = RESULTS_DIR / f"insight-{today}.txt"

    # Don't regenerate if already done today
    if output_path.exists():
        print(f"Insight already generated today: {output_path}")
        print(output_path.read_text())
        return

    insight = generate_insight()
    output_path.write_text(insight)
    print(f"Daily insight → {output_path}")
    print(insight)


if __name__ == "__main__":
    main()
