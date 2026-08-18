#!/usr/bin/env python3
"""A fired-and-expired one-shot cron must not count as expected-but-missing.

THE DEFECT (observed live 2026-08-18). CronCreate auto-deletes a one-shot
(`recurring: false`) once it fires — correct, documented behavior, not a
registration failure. `session-crons`' `expected` count didn't know that: a
same-day deadline reminder that had already fired that morning stayed counted
forever, producing a standing false "N/N+1 registered" warn on an otherwise
fully-healthy host (the SAME pass that installed the launchd runner to fix the
real gap for the other one-shots that hadn't fired yet).

`_one_shot_already_spent()` closes it by anchoring the cron's singleton
minute/hour/day/month to the current year (crons.json carries no year field)
and excluding the entry from `expected` only once that date is in the past —
mirroring the existing `_entry_marked_parked` + `_cron_can_never_fire`
"both signals required" shape one function up.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "health-check.py"
SPEC = importlib.util.spec_from_file_location("health_check", SCRIPT)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

# 2026-08-18 09:00 America/... local — a fixed "now" so the fixture dates are
# deterministic regardless of when the suite runs.
import time  # noqa: E402
NOW = time.mktime((2026, 8, 18, 9, 0, 0, 0, 0, -1))


def _workspace(root: Path, entries, registered: int) -> Path:
    workspace = root / "workspace"
    config = workspace / "hosts" / "test-host" / "crons.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(entries))
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "session-starts.log").write_text(
        json.dumps({"host": "test-host", "session_started_at": NOW - 3600}) + "\n"
    )
    (config.parent / "schedule-crons-stamp.json").write_text(
        json.dumps({"ts": NOW - 60, "registered": registered, "config_total": len(entries)})
    )
    return workspace


def _check(workspace, now=NOW):
    with mock.patch.object(health, "_local_host_labels", return_value={"test-host"}):
        return health.check_session_cron_registration(
            workspace, host_label="test-host", runtime="claude", now=now,
        )


class OneShotExpiryTest(unittest.TestCase):
    def test_spent_one_shot_is_not_expected(self):
        """A one-shot dated EARLIER TODAY than `now` must not inflate `expected`."""
        entries = [
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {
                "name": "replit-deletion-20260818-final",
                "cron": "13 9 18 8 *",  # 09:13 today; `now` is 09:00... below fires it later
                "recurring": False,
                "prompt": "x",
            },
        ]
        # `now` past the one-shot's fire time (09:13) — it already fired and
        # auto-deleted from CronList, so only main-loop remains registered.
        workspace = _workspace(root=Path(tempfile.mkdtemp()), entries=entries, registered=1)
        check = _check(workspace, now=NOW + 3600)
        self.assertEqual(check["status"], "ok", check)
        self.assertIn("1 session cron", check["detail"])

    def test_not_yet_fired_one_shot_still_expected(self):
        """Control: a one-shot dated LATER than `now` must still count — only a
        PAST one-shot is exempt, not one-shots in general."""
        entries = [
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {
                "name": "heroku-deletion-20260827",
                "cron": "9 9 27 8 *",  # 9 days after `now`
                "recurring": False,
                "prompt": "x",
            },
        ]
        # Only main-loop registered, but the one-shot hasn't fired yet, so it's
        # still expected — this must warn, not silently drop to "ok".
        workspace = _workspace(root=Path(tempfile.mkdtemp()), entries=entries, registered=1)
        check = _check(workspace, now=NOW)
        self.assertEqual(check["status"], "warn", check)

    def test_recurring_entry_with_past_looking_date_fields_never_exempt(self):
        """A `recurring: true` (or absent) entry is NEVER exempted by this path —
        only the explicit `recurring: false` marker triggers the date check.
        A daily/weekly cron's dom/month fields being "in the past" is
        meaningless for a recurring schedule."""
        entries = [
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "annual-thing", "cron": "0 9 1 1 *", "prompt": "x"},  # Jan 1 — long past `now`
        ]
        workspace = _workspace(root=Path(tempfile.mkdtemp()), entries=entries, registered=1)
        check = _check(workspace, now=NOW)
        self.assertEqual(check["status"], "warn", check)

    def test_ranged_or_listed_one_shot_field_left_conservative(self):
        """A one-shot with a non-singleton field (unusual, but not our business
        to guess) must NOT be excluded — stay counted as expected rather than
        risk a wrong verdict."""
        entries = [
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {
                "name": "weird-one-shot",
                "cron": "0 9 1,2 8 *",  # listed dom field
                "recurring": False,
                "prompt": "x",
            },
        ]
        workspace = _workspace(root=Path(tempfile.mkdtemp()), entries=entries, registered=1)
        check = _check(workspace, now=NOW + 365 * 86400)  # a year later either way
        self.assertEqual(check["status"], "warn", check)

    def test_matched_case_stays_ok(self):
        """Control: when a spent one-shot IS excluded and everything else IS
        registered, the probe must be quiet — this is the real-world shape the
        defect broke (11/11 correctly registered, still warned "11/12")."""
        entries = [
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "digest", "cron": "2 6 * * *", "prompt": "run"},
            {
                "name": "replit-deletion-20260818-final",
                "cron": "13 9 18 8 *",
                "recurring": False,
                "prompt": "x",
            },
        ]
        workspace = _workspace(root=Path(tempfile.mkdtemp()), entries=entries, registered=2)
        check = _check(workspace, now=NOW + 3600)
        self.assertEqual(check["status"], "ok", check)


if __name__ == "__main__":
    unittest.main()
