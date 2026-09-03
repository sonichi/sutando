#!/usr/bin/env python3
"""Publish the agent's durable schedule (crons.json + each scheduler's own
last-fire record) as the Room Context doc the AG2 Space "Scheduled" Activity
tab reads. Schedule evaluation, file shape and fire records come from
src/dashboard_schedules (the schedule domain owner); this file only renders
and publishes. Owner-greenlit 2026-08-21: source is durable crons.json."""
from __future__ import annotations
import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "src"))
import dashboard_schedules as ds  # noqa: E402

# Wide enough that a leap-day job (29 Feb) always has a next fire inside it.
PANEL_HORIZON_DAYS = 366 * 4 + 1


class SourceUnavailable(RuntimeError):
    """The schedule source could not be resolved or read. Distinct from an
    empty schedule: nothing may be published on it."""


def _cfg(arg: str) -> str:
    proc = subprocess.run(["bash", os.path.join(REPO, "scripts/sutando-config.sh"), arg],
                          capture_output=True, text=True)
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        raise SourceUnavailable(
            f"sutando-config.sh {arg}: rc={proc.returncode} {proc.stderr.strip()[:200]}")
    return value


def read_crons_strict(path: Path) -> list:
    """The job list, or SourceUnavailable when the file is missing, unreadable
    or not a JSON list. A valid empty list is an empty schedule and is returned."""
    p = Path(path)
    try:
        jobs = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise SourceUnavailable(f"{p}: {exc}") from exc
    if not isinstance(jobs, list):
        raise SourceUnavailable(f"{p}: expected a JSON list, got {type(jobs).__name__}")
    return jobs


def _human_cron(expr: str) -> str:
    # Only an expression with no calendar restriction gets a short form; any
    # restricted dom/month/dow field is shown verbatim so nothing is hidden.
    p = expr.split()
    if len(p) != 5:
        return expr
    m, h, dom, mon, dow = p
    unrestricted = dom == "*" and mon == "*" and dow == "*"
    if m.startswith("*/") and h == "*" and unrestricted:
        return f"every {m[2:]} min"
    if ("," in m or "-" in m) and h == "*" and unrestricted:
        return f"~every few min ({m})"
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        return (f"daily {int(h):02d}:{int(m):02d}" if dow == "*"
                else f"{dow} {int(h):02d}:{int(m):02d}")
    return expr


def _fmt(dt) -> str:
    if not isinstance(dt, datetime.datetime):
        return "—"
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def build_rows(now: datetime.datetime | None = None) -> list[dict]:
    ws, host = _cfg("workspace"), _cfg("host-label")
    jobs = read_crons_strict(Path(ws) / "hosts" / host / "crons.json")  # canonical shape only
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = Path(ws) / "state"
    rows = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        expr = j.get("cron") or ""
        dyn = j.get("loop") == "dynamic"
        rows.append({
            "name": j.get("name", "?"),
            "schedule": "adaptive (self-paced)" if dyn else _human_cron(expr),
            "owner": ds.schedule_owner(j),
            "next_fire": _fmt(ds.next_run_for_job(j, now, PANEL_HORIZON_DAYS)),
            "last_fired": _fmt(ds.last_fired(j, state)),
            "does": j.get("prompt_skill") or (j.get("prompt") or "")[:48] or "—",
        })
    return rows


def render_md(rows: list[dict]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    out = ["# Scheduled jobs", f"*updated {now} — source: durable crons.json; times UTC*", "",
           "| Job | Schedule | Fires via | Last fired | Next fire | Does |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['name']} | {r['schedule']} | {r['owner']} | {r['last_fired']} "
                   f"| {r['next_fire']} | {r['does']} |")
    return "\n".join(out) + "\n"


SCHED_FOLDER = "activity"   # client "Scheduled" tab reads folder=activity name=SCHEDULE.md
SCHED_NAME = "SCHEDULE.md"


def publish(room: str, content: str) -> dict:
    # Reuse the gateway room-ops doc client; room is caller-supplied (no id baked in).
    ops = os.path.join(REPO, "skills/agent-room-ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)   # doc.py does a sibling-relative `from _gateway import ...`
    spec = importlib.util.spec_from_file_location("_ops_doc", os.path.join(ops, "doc.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.doc_put(room, content, folder=SCHED_FOLDER, name=SCHED_NAME,
                       message="scheduled-jobs refresh")


def _arg(flag: str):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main(argv: list[str]) -> int:
    try:
        rows = build_rows()
    except SourceUnavailable as exc:
        # Unknown is not empty: the room doc keeps its last good content.
        print(f"refusing to publish: schedule source unavailable ({exc})", file=sys.stderr)
        return 2
    if "--json" in argv:
        print(json.dumps(rows, indent=2))
        return 0
    md = render_md(rows)
    room = argv[argv.index("--room") + 1] if "--room" in argv else None
    if "--publish" in argv and room:
        r = publish(room, md)
        if r.get("ok"):
            print("published")
            return 0
        print(f"FAILED: {r.get('reason')}")
        return 1
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
