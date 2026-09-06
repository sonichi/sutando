#!/usr/bin/env python3
"""Report how many tasks were completed each day, from the durable per-day
history that hooks/stamp-task-id.py maintains (`state/task-completions-daily.json`).

The daily counter (`state/task-counter.json`) only knows *today's* count — it
resets each day. The history file accumulates one entry per day and never loses a
past day, so this is what makes "how many tasks did I complete every day?"
answerable over time.

Usage:
  python3 scripts/task-completions.py           # last 14 days, human-readable
  python3 scripts/task-completions.py --days 30 # last 30 days
  python3 scripts/task-completions.py --all      # every recorded day
  python3 scripts/task-completions.py --json     # machine-readable {day: count}
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# The sanctioned resolver owns all fallback/override logic; never reconstruct a
# workspace path inline here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WS = Path(resolve_workspace())

HISTORY = WS / "state" / "task-completions-daily.json"
COUNTER = WS / "state" / "task-counter.json"


def load_history() -> dict[str, int]:
    """{YYYYMMDD: count}. Merges the live counter's today value so a report run
    mid-day reflects the current total even if the history file lags."""
    hist: dict[str, int] = {}
    try:
        raw = json.load(open(HISTORY))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and len(k) == 8 and k.isdigit():
                    try:
                        hist[k] = int(v)
                    except (TypeError, ValueError):
                        continue
    except Exception:
        pass
    # Fold in today's live counter value if it's ahead of the recorded history.
    try:
        c = json.load(open(COUNTER))
        d, n = c.get("date"), int(c.get("count", 0))
        if isinstance(d, str) and len(d) == 8 and d.isdigit():
            hist[d] = max(hist.get(d, 0), n)
    except Exception:
        pass
    return hist


def _fmt_day(ymd: str) -> str:
    try:
        return datetime.datetime.strptime(ymd, "%Y%m%d").strftime("%Y-%m-%d (%a)")
    except ValueError:
        return ymd


def render(hist: dict[str, int], days: int | None) -> str:
    if not hist:
        return "No task completions recorded yet."
    ordered = sorted(hist.items(), reverse=True)  # newest first
    if days is None:
        shown = ordered
    else:
        # CALENDAR days, not the newest N entries: empty days are absent from the
        # history, so a slice reaches further back than N days whenever there are gaps.
        cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).strftime("%Y%m%d")
        shown = [(ymd, n) for ymd, n in ordered if ymd >= cutoff]
    today = datetime.date.today().strftime("%Y%m%d")
    lines = ["Task completions by day:"]
    for ymd, n in shown:
        mark = "  <- today" if ymd == today else ""
        lines.append(f"  {_fmt_day(ymd)}: {n}{mark}")
    total = sum(n for _, n in shown)
    label = "all recorded days" if days is None else f"last {days} day(s)"
    lines.append(f"  ── total ({label}): {total}")
    lines.append(f"  today: {hist.get(today, 0)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report tasks completed per day.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=14, help="how many recent days to show (default 14)")
    g.add_argument("--all", action="store_true", help="show every recorded day")
    ap.add_argument("--json", action="store_true", help="emit raw {day: count} JSON")
    args = ap.parse_args(argv)

    hist = load_history()
    if args.json:
        print(json.dumps(hist, sort_keys=True))
        return 0
    print(render(hist, None if args.all else args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
