#!/usr/bin/env python3
"""Call statistics — summarize phone call activity over a time window.

Usage:
    python3 src/call-stats.py                  # last 7 days
    python3 src/call-stats.py --days 30        # last 30 days
    python3 src/call-stats.py --all            # all time
    python3 src/call-stats.py --json           # machine-readable

Complements daily-insight.py. daily-insight picks ONE actionable pattern;
call-stats dumps the full picture for weekly/monthly review.

Reads results/calls/calls.jsonl. Expects enriched fields (duration_seconds,
caller, purpose, is_meeting, start_time) from PR #209 onward. Gracefully
falls back for older entries that only have callSid/transcript/timestamp.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CALLS_FILE = Path(__file__).parent.parent / "results" / "calls" / "calls.jsonl"


def load_calls():
    if not CALLS_FILE.exists():
        return []
    calls = []
    for line in CALLS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return calls


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def mask_phone(num):
    """+14256716122 → +1-425-XXX-XXXX (keep country + area code)."""
    if not num or num == "unknown":
        return num or "unknown"
    digits = "".join(c for c in num if c.isdigit())
    if len(digits) >= 10:
        return f"+{digits[0]}-{digits[1:4]}-XXX-XXXX" if len(digits) == 11 else f"+{digits[:-10]}-{digits[-10:-7]}-XXX-XXXX"
    return num


def filter_by_window(calls, days):
    if days is None:
        return calls
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for c in calls:
        ts = parse_ts(c.get("start_time") or c.get("timestamp"))
        if ts and ts >= cutoff:
            out.append(c)
    return out


def compute_stats(calls):
    total = len(calls)
    durations = [c["duration_seconds"] for c in calls if isinstance(c.get("duration_seconds"), (int, float)) and c["duration_seconds"] > 0]
    hours = Counter()
    days = Counter()
    purposes = Counter()
    callers = Counter()
    meetings = 0
    owner_calls = 0
    for c in calls:
        ts = parse_ts(c.get("start_time") or c.get("timestamp"))
        if ts:
            hours[ts.hour] += 1
            days[ts.strftime("%a")] += 1
        purposes[c.get("purpose") or "unknown"] += 1
        callers[c.get("caller") or "unknown"] += 1
        if c.get("is_meeting"):
            meetings += 1
        if c.get("is_owner"):
            owner_calls += 1

    avg_dur = sum(durations) / len(durations) if durations else 0
    longest = max(durations) if durations else 0
    shortest = min(durations) if durations else 0

    return {
        "total": total,
        "with_duration": len(durations),
        "avg_duration_seconds": round(avg_dur, 1),
        "longest_seconds": longest,
        "shortest_seconds": shortest,
        "total_minutes": round(sum(durations) / 60, 1),
        "meetings": meetings,
        "owner_calls": owner_calls,
        "peak_hour": hours.most_common(1)[0] if hours else None,
        "quiet_hours": [h for h in range(8, 22) if hours.get(h, 0) == 0],
        "busiest_day": days.most_common(1)[0] if days else None,
        "top_purposes": purposes.most_common(5),
        "top_callers": [(mask_phone(n), c) for n, c in callers.most_common(5)],
    }


def format_text(stats, window_label):
    lines = [f"📞 Call stats — {window_label}", ""]
    lines.append(f"Total: {stats['total']} calls")
    if stats["with_duration"]:
        lines.append(f"  With duration data: {stats['with_duration']} ({stats['total_minutes']} min total)")
        lines.append(f"  Avg: {stats['avg_duration_seconds']}s | Longest: {stats['longest_seconds']}s | Shortest: {stats['shortest_seconds']}s")
    else:
        lines.append("  (no duration data yet — enriched logging starts post-#209)")
    if stats["meetings"]:
        lines.append(f"  Meetings: {stats['meetings']}")
    if stats["owner_calls"]:
        lines.append(f"  Owner calls: {stats['owner_calls']} / {stats['total']}")

    if stats["peak_hour"]:
        h, n = stats["peak_hour"]
        lines.append(f"\n⏰ Peak hour: {h:02d}:00 ({n} calls)")
    if stats["quiet_hours"]:
        lines.append(f"   Quiet hours (8-21): {', '.join(f'{h:02d}:00' for h in stats['quiet_hours'][:5])}")
    if stats["busiest_day"]:
        d, n = stats["busiest_day"]
        lines.append(f"📅 Busiest day: {d} ({n} calls)")

    if stats["top_purposes"] and stats["top_purposes"][0][0] != "unknown":
        lines.append("\n🎯 Purposes:")
        for p, n in stats["top_purposes"]:
            lines.append(f"   {p}: {n}")

    if stats["top_callers"] and stats["top_callers"][0][0] != "unknown":
        lines.append("\n☎️  Top callers:")
        for num, n in stats["top_callers"]:
            lines.append(f"   {num}: {n}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Window in days (default 7)")
    ap.add_argument("--all", action="store_true", help="All time (overrides --days)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    calls = load_calls()
    if args.all:
        filtered = calls
        window = "all time"
    else:
        filtered = filter_by_window(calls, args.days)
        window = f"last {args.days} days"

    if not filtered:
        print(f"No calls in {window}.", file=sys.stderr)
        if args.json:
            print(json.dumps({"total": 0, "window": window}))
        return 0

    stats = compute_stats(filtered)
    if args.json:
        stats["window"] = window
        print(json.dumps(stats, default=str))
    else:
        print(format_text(stats, window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
