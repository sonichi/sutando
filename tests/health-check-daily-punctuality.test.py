#!/usr/bin/env python3
"""Lateness is the signal: a file produced daily by another path looks identical to
one produced by a working schedule."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def job(name, hour, minute, arts, today_seen=True, since_due=0):
    return {"name": name, "hour": hour, "minute": minute, "artifacts": arts,
            "today_seen": today_seen, "minutes_since_due": since_due}


DUE = 6 * 60 + 50


class TestLatenessIsTheSignal(unittest.TestCase):
    def test_on_schedule_is_ok(self):
        arts = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]
        r = hc._interpret_daily_punctuality([job("daily-insight", 6, 50, arts)])
        self.assertEqual(r["status"], "ok", r)

    def test_the_real_host_shape_warns(self):
        """Verbatim from this host: 07:12 08:37 07:53 07:20 07:32 07:38 07:27."""
        mins = [7 * 60 + 12, 8 * 60 + 37, 7 * 60 + 53, 7 * 60 + 20,
                7 * 60 + 32, 7 * 60 + 38, 7 * 60 + 27]
        arts = [(f"2026-08-0{i+1}", m) for i, m in enumerate(mins)]
        r = hc._interpret_daily_punctuality([job("daily-insight", 6, 50, arts)])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("+42 min late", r["detail"])
        self.assertIn("covering for it", r["detail"],
                      "must say the schedule is not what produced these")

    def test_median_not_mean_so_one_outlier_does_not_flip_it(self):
        arts = [("2026-08-0%d" % i, DUE + 2) for i in range(1, 7)] + [("2026-08-07", DUE + 300)]
        r = hc._interpret_daily_punctuality([job("daily-insight", 6, 50, arts)])
        self.assertEqual(r["status"], "ok", "a single late run is not a dead schedule")

    def test_even_sample_uses_the_true_median_not_the_upper_middle(self):
        """[+0, +30] has median +15, inside tolerance; picking deltas[n//2] gave +30
        and warned on a job that was on time half the days."""
        arts = [("2026-08-07", DUE), ("2026-08-08", DUE + 30)]
        r = hc._interpret_daily_punctuality([job("daily-insight", 6, 50, arts)])
        self.assertEqual(r["status"], "ok", r)
        self.assertNotIn("late", r["detail"])

    def test_within_tolerance_is_ok(self):
        arts = [(f"2026-08-0{i}", DUE + hc.DAILY_LATE_TOLERANCE_MIN - 1) for i in range(1, 8)]
        self.assertEqual(
            hc._interpret_daily_punctuality([job("x", 6, 50, arts)])["status"], "ok")


class TestMissedToday(unittest.TestCase):
    def test_no_output_today_past_grace_warns(self):
        arts = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]
        r = hc._interpret_daily_punctuality(
            [job("morning-briefing", 6, 57, arts, today_seen=False,
                 since_due=hc.DAILY_MISS_GRACE_MIN + 1)])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("no output today", r["detail"])

    def test_within_grace_is_not_yet_a_miss(self):
        arts = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]
        r = hc._interpret_daily_punctuality(
            [job("morning-briefing", 6, 57, arts, today_seen=False, since_due=5)])
        self.assertEqual(r["status"], "ok", "do not cry miss inside the grace window")


class TestUnverifiableIsNotClean(unittest.TestCase):
    def test_a_job_with_no_dated_artifact_is_named_not_silently_passed(self):
        """The fail-open: no artifact means UNKNOWN, and the detail must say so."""
        r = hc._interpret_daily_punctuality([job("morning-briefing", 6, 57, [])])
        self.assertEqual(r["status"], "ok", "unknown is not a failure on its own")
        self.assertIn("no dated artifact", r["detail"])
        self.assertIn("morning-briefing", r["detail"],
                      "name the job whose punctuality cannot be checked")

    def test_unverifiable_is_still_named_alongside_a_real_warn(self):
        mins = [7 * 60 + 30] * 7
        arts = [(f"2026-08-0{i+1}", m) for i, m in enumerate(mins)]
        r = hc._interpret_daily_punctuality(
            [job("daily-insight", 6, 50, arts), job("morning-briefing", 6, 57, [])])
        self.assertEqual(r["status"], "warn")
        self.assertIn("daily-insight", r["detail"])
        self.assertIn("morning-briefing", r["detail"],
                      "a warn must not swallow the unverifiable job")


class TestArtifactDiscovery(unittest.TestCase):
    def test_month_bucketed_archive_is_found(self):
        """Delivered results are archived into results/archive/YYYY-MM/, so the
        durable copies sit two levels down."""
        with tempfile.TemporaryDirectory() as td:
            res = Path(td) / "results"
            (res / "archive" / "2026-08").mkdir(parents=True)
            deep = res / "archive" / "2026-08" / "insight-2026-08-08.txt"
            deep.write_text("x")
            os.utime(deep, (time.time(), time.mktime((2026, 8, 8, 7, 27, 0, 0, 0, -1))))
            got = hc._daily_artifact_minutes(res, "insight")
            self.assertEqual([d for d, _ in got], ["2026-08-08"],
                            "must find the month-bucketed archive copy")
            self.assertEqual(got[0][1], 7 * 60 + 27)

    def test_unrelated_and_undated_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            res = Path(td) / "results"
            res.mkdir()
            # "insight-2099abc" passes the GLOB (insight-20*) and fails the date
            # regex — the only way to reach that branch, since the glob filters first.
            for n in ("insight-notadate.txt", "briefing-2026-08-08.txt", "insight.txt",
                      "insight-2099abc.txt"):
                (res / n).write_text("x")
            self.assertEqual(hc._daily_artifact_minutes(res, "insight"), [])

    def test_missing_results_dir_returns_empty_not_an_exception(self):
        self.assertEqual(hc._daily_artifact_minutes(Path("/nonexistent-xyz"), "insight"), [])


class TestCollector(unittest.TestCase):
    """The collector's FILTERING is where a job silently drops out of scope."""

    def _run(self, entries, artifacts=()):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "hosts" / "H").mkdir(parents=True)
            if entries is not None:
                (ws / "hosts" / "H" / "crons.json").write_text(entries)
            res = ws / "results"
            res.mkdir()
            for name, when in artifacts:
                f = res / name
                f.write_text("x")
                os.utime(f, (time.time(), time.mktime(when)))
            with mock.patch.object(hc, "WORKSPACE_DIR", ws), \
                 mock.patch.object(hc, "_host_label", lambda: "H"):
                return hc.check_daily_cron_punctuality()

    def test_no_crons_file_is_skipped_not_ok_silent(self):
        r = self._run(None)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no per-host crons.json", r["detail"])

    def test_unreadable_crons_file_says_so(self):
        r = self._run("{not json")
        self.assertEqual(r["status"], "ok")
        self.assertIn("unreadable", r["detail"])

    def test_a_scalar_root_degrades_instead_of_aborting_the_health_run(self):
        """`1` is valid JSON, so json.loads succeeds and raw.get() raised
        AttributeError out of the always-on run_all_checks() path."""
        r = self._run("1")
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("root is int", r["detail"])

    def test_launchd_and_codex_entries_are_out_of_scope(self):
        r = self._run(json.dumps([
            {"name": "a", "cron": "50 6 * * *", "launchd": True},
            {"name": "b", "cron": "57 6 * * *", "execution": "codex-task"},
        ]))
        self.assertIn("no session-owned daily jobs", r["detail"],
                      "these have their own runner and must not be scored here")

    def test_sub_daily_and_non_every_day_schedules_are_out_of_scope(self):
        r = self._run(json.dumps([
            {"name": "loop", "cron": "*/5 * * * *"},
            {"name": "weekly", "cron": "0 9 * * 1"},
            {"name": "monthly", "cron": "0 9 1 * *"},
            {"name": "dynamic"},
        ]))
        self.assertIn("no session-owned daily jobs", r["detail"])

    def test_end_to_end_late_daily_job_warns(self):
        arts = [(f"insight-2026-08-0{d}.txt", (2026, 8, d, 7, 30, 0, 0, 0, -1))
                for d in range(1, 8)]
        r = self._run(json.dumps([{"name": "daily-insight", "cron": "50 6 * * *"}]), arts)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("late", r["detail"])

    def test_a_malformed_time_degrades_instead_of_crashing(self):
        """`isdigit()` passes "61"/"24"; dtime() would raise out of run_all_checks()."""
        r = self._run(json.dumps([{"name": "bad", "cron": "61 24 * * *"},
                                  {"name": "daily-insight", "cron": "50 6 * * *"}]))
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("unparseable daily cron time", r["detail"])
        self.assertIn("bad", r["detail"], "name the offending entry")

    def test_a_dict_shaped_config_is_read_too(self):
        r = self._run(json.dumps({"crons": [{"name": "x", "cron": "*/5 * * * *"}]}))
        self.assertIn("no session-owned daily jobs", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
