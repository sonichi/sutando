#!/usr/bin/env python3
"""Schedule surface for the Sutando Server: schedule.list.

Thin binding over the schedules DOMAIN module (src/dashboard_schedules.py) —
the same read policy the dashboard's Schedules card delegates to. The composer
injects the resolved crons.json path (workspace + canonical host label); this
view never resolves workspace/host itself, and never filters: every entry is
returned, tagged with the scheduler that owns it (session / launchd / codex /
dynamic-loop) so a client menu can render them distinctly.

crons.json IS the canonical job list (skills/schedule-crons/SKILL.md): live
session cron registrations are snapshots of this file, so there is no separate
daemon-reachable session state to merge.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # src/
import dashboard_schedules  # noqa: E402


class SchedulesView:
    def __init__(self, crons_path: str | Path):
        self.crons_path = Path(crons_path)

    # ── schedule.list ───────────────────────────────────────────────────────
    def list_schedules(self) -> dict:
        """{schedules: [...]} — [] for a missing/empty crons.json, never an
        error (an unconfigured host simply has no schedules)."""
        return {"schedules": dashboard_schedules.list_schedules(self.crons_path)}
