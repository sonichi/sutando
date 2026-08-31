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
from datetime import datetime, timedelta
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


def job(name, hour, minute, arts, today_seen=True, since_due=0, conditional=False):
    return {"name": name, "hour": hour, "minute": minute, "artifacts": arts,
            "today_seen": today_seen, "minutes_since_due": since_due,
            "conditional": conditional, "stem_declared": False}


DUE = 6 * 60 + 50


class TestLatenessIsTheSignal(unittest.TestCase):
    def test_on_schedule_is_ok(self):
        arts = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]
        r = hc._interpret_daily_punctuality([job("daily-insight", 6, 50, arts)])
        self.assertEqual(r["status"], "ok", r)

    def test_partial_coverage_is_not_ok_even_when_every_observed_job_is_punctual(self):
        """The reviewer's 1-of-5 host: four UNCHECKED jobs can miss forever
        behind a green status, which is the class this probe exists to catch."""
        good = [(f"2026-08-{d:02d}", 361) for d in range(6, 13)]
        jobs = [job("seen", 6, 0, good)] + [job(f"blind{i}", 6, 0, []) for i in range(4)]
        r = hc._interpret_daily_punctuality(jobs)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("1 of 5", r["detail"])
        self.assertIn("cannot be ok", r["detail"])

    def test_full_coverage_all_punctual_is_still_ok(self):
        """Coverage gates the verdict; it must not make the probe permanently warn."""
        good = [(f"2026-08-{d:02d}", 361) for d in range(6, 13)]
        r = hc._interpret_daily_punctuality([job(f"j{i}", 6, 0, good) for i in range(3)])
        self.assertEqual(r["status"], "ok", r["detail"])

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
        """Unknown is not a FAILURE, but it is not `ok` either — with nothing
        observable the probe has no coverage, and green would claim it does."""
        r = hc._interpret_daily_punctuality([job("morning-briefing", 6, 57, [])])
        self.assertNotEqual(r["status"], "ok", "verified nothing, so green is a false claim")
        self.assertIn("no dated artifact", r["detail"])
        self.assertIn("morning-briefing", r["detail"],
                      "name the job whose punctuality cannot be checked")

    def test_an_all_unobservable_set_states_its_coverage_not_a_clean_bill(self):
        """Nothing observable: the probe must not report health it never measured.
        Dashboards read status, so the honest detail alone was not enough."""
        r = hc._interpret_daily_punctuality(
            [job("morning-briefing", 6, 57, [], today_seen=False, since_due=300)])
        self.assertIn("0 of 1 daily job(s) observable", r["detail"])
        self.assertIn("UNCHECKED", r["detail"])
        self.assertNotIn("landing on schedule", r["detail"],
                         "nothing was observed, so nothing landed on schedule")
        self.assertEqual(r["status"], "warn",
                         "verified 0 jobs, so `ok` is a health claim about nothing")

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

    def _run(self, entries, artifacts=(), sentinels=()):
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
            if sentinels:
                st = ws / "state"
                st.mkdir(exist_ok=True)
                # Body format is production's: morning-briefing.py writes
                # `datetime.now().isoformat()` into state/<job>-<date>.sentinel.
                for name, stamp in sentinels:
                    (st / name).write_text(stamp)
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

    def test_codex_entries_are_out_of_scope(self):
        """Codex-task jobs complete in another runtime that writes no sentinel,
        so there is nothing here that could observe them."""
        r = self._run(json.dumps([
            {"name": "b", "cron": "57 6 * * *", "execution": "codex-task"},
        ]))
        self.assertIn("no plain-daily jobs", r["detail"],
                      "that runner owns them and must not be scored here")

    def test_launchd_entries_are_in_scope_via_their_completion_sentinel(self):
        """Reverses the previous exclusion: the launchd lane publishes no dated
        results file, and its sentinel is the only record that it finished."""
        r = self._run(json.dumps([
            {"name": "a", "cron": "50 6 * * *", "launchd": True},
        ]))
        self.assertIn("0 of 1 daily job(s) observable", r["detail"],
                      "in the population, and UNCHECKED until it stamps")
        self.assertNotIn("median", r["detail"],
                         "a job that never stamps must not warn, only stay unchecked")

    def test_sub_daily_and_non_every_day_schedules_are_out_of_scope(self):
        r = self._run(json.dumps([
            {"name": "loop", "cron": "*/5 * * * *"},
            {"name": "weekly", "cron": "0 9 * * 1"},
            {"name": "monthly", "cron": "0 9 1 * *"},
            {"name": "dynamic"},
        ]))
        self.assertIn("no plain-daily jobs", r["detail"])

    def test_end_to_end_late_daily_job_warns(self):
        # Dates are RELATIVE: a fixed window ages until the job reads as
        # naming-drift rather than lateness, which is a different verdict.
        days = [datetime.now().date() - timedelta(days=k) for k in range(7, 0, -1)]
        arts = [(f"insight-{d.isoformat()}.txt",
                 (d.year, d.month, d.day, 7, 30, 0, 0, 0, -1)) for d in days]
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
        self.assertIn("no plain-daily jobs", r["detail"])

    def test_a_declared_artifact_beats_the_name_derived_stem(self):
        """`talk-events-nightly` infers stem `nightly`, so its real
        fleet-growth-<date>.mp4 is never observed and the job is UNCHECKED
        forever — invisible behind a status that is not a failure."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        arts = [(f"fleet-growth-{today}.mp4", now.timetuple())]
        entry = {"name": "talk-events-nightly", "cron": f"{now.minute} {now.hour} * * *"}
        inferred = self._run(json.dumps({"crons": [entry]}), arts)
        declared = self._run(
            json.dumps({"crons": [dict(entry, artifact="fleet-growth")]}), arts)
        self.assertIn("UNCHECKED", inferred["detail"])
        self.assertNotIn("UNCHECKED", declared["detail"])
        self.assertEqual(declared["status"], "ok", declared["detail"])

    def test_an_inferred_stem_that_matched_nothing_never_reports_a_miss(self):
        """A wrong stem and a job that did not run are the same observation.
        Calling it missed would alarm on a job the probe cannot even see."""
        entry = {"name": "talk-events-nightly", "cron": "0 3 * * *"}
        r = self._run(json.dumps({"crons": [entry]}), ())
        self.assertNotIn("no output today", r["detail"])
        self.assertIn("UNCHECKED", r["detail"])


class TestSentinelFallbackForSessionOwnedJobs(unittest.TestCase):
    """`launchd` conflates two properties: "runs under launchd" and "publishes
    no dated results file". A session-owned job can be the second without the
    first, and then it reports UNCHECKED forever while its dated completion
    sentinels sit unread in state/."""

    _run = TestCollector._run

    @staticmethod
    def _week(job, hour, minute):
        # range must include TODAY: a window ending yesterday reads as missed-today.
        days = [datetime.now().date() - timedelta(days=k) for k in range(6, -1, -1)]
        return [(f"{job}-{d.isoformat()}.sentinel",
                 datetime(d.year, d.month, d.day, hour, minute, 1).isoformat())
                for d in days]

    def test_a_session_owned_job_is_scored_from_its_completion_sentinel(self):
        r = self._run(
            json.dumps([{"name": "morning-briefing", "cron": "57 6 * * *"}]),
            sentinels=self._week("morning-briefing", 7, 0),
        )
        self.assertNotIn("UNCHECKED", r["detail"],
                         "7 dated sentinels exist; the job is observable")
        self.assertIn("1 of 1 daily job(s) observable", r["detail"])
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_results_artifacts_still_win_when_present(self):
        """The fallback fires only on an EMPTY results/ glob, so a job that does
        publish there keeps its existing source and cannot be double-read."""
        days = [datetime.now().date() - timedelta(days=k) for k in range(6, -1, -1)]
        arts = [(f"insight-{d.isoformat()}.txt",
                 (d.year, d.month, d.day, 6, 51, 0, 0, 0, -1)) for d in days]
        r = self._run(
            json.dumps([{"name": "daily-insight", "cron": "50 6 * * *"}]),
            artifacts=arts,
            sentinels=self._week("daily-insight", 23, 59),
        )
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertNotIn("late", r["detail"],
                         "scored from results/ (+1 min), not the 23:59 sentinels")

    def test_a_launchd_job_is_unaffected(self):
        """It already took the sentinel branch; the fallback must not re-read it."""
        r = self._run(
            json.dumps([{"name": "digest", "cron": "0 6 * * *", "launchd": True}]),
            sentinels=self._week("digest", 6, 1),
        )
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("1 of 1 daily job(s) observable", r["detail"])


class TestConditionalProducers(unittest.TestCase):
    """A job that renders only when new input exists produces nothing on a quiet
    day. Absence is then evidence of nothing, not evidence of a miss."""

    def test_the_reviewers_control_prior_artifacts_none_today_120min_late(self):
        prior = [(f"2026-08-{d:02d}", 361) for d in range(6, 13)]
        unconditional = job("render", 6, 0, prior, today_seen=False, since_due=120)
        self.assertEqual(
            hc._interpret_daily_punctuality([unconditional])["status"], "warn",
            "an UNCONDITIONAL producer with no output today must still warn")

        conditional = job("render", 6, 0, prior, today_seen=False, since_due=120,
                          conditional=True)
        r = hc._interpret_daily_punctuality([conditional])
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertNotIn("no output today", r["detail"])

    def test_a_conditional_producer_that_has_produced_NOTHING_is_not_a_blind_spot(self):
        """The quiet stretch can cover the whole window, and often does.

        A threshold guard or an opt-in job may legitimately produce nothing for
        months, so `conditional` has to excuse an empty artifact set too - not
        only a single quiet day. Otherwise the flag can never reach `ok` on the
        hosts it exists for, which is the standing warning #2754 rules out.
        """
        r = hc._interpret_daily_punctuality(
            [job("rotate-log", 4, 23, [], today_seen=False, since_due=300,
                 conditional=True)])
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertNotIn("UNCHECKED", r["detail"])

    def test_an_unconditional_producer_with_no_artifacts_still_gates(self):
        """The control that must keep failing: without the declaration an empty
        artifact set is a real blind spot, and green would claim coverage."""
        r = hc._interpret_daily_punctuality(
            [job("mystery", 4, 23, [], today_seen=False, since_due=300)])
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("UNCHECKED", r["detail"])

    def test_a_quiet_conditional_job_does_not_hide_a_real_warn(self):
        """Excusing it must not also silence a genuinely late sibling."""
        late = [(f"2026-08-{d:02d}", 6 * 60 + 200) for d in range(6, 13)]
        r = hc._interpret_daily_punctuality(
            [job("render", 6, 0, late),
             job("rotate-log", 4, 23, [], today_seen=False, since_due=300,
                 conditional=True)])
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("render", r["detail"])

    def test_conditional_does_not_suppress_LATENESS(self):
        """Only absence is excused. A conditional job that renders consistently
        late is still late, and that signal must survive the exemption."""
        late = [(f"2026-08-{d:02d}", 6 * 60 + 0 + 200) for d in range(6, 13)]
        r = hc._interpret_daily_punctuality(
            [job("render", 6, 0, late, today_seen=True, conditional=True)])
        self.assertEqual(r["status"], "warn", r["detail"])



class TestMidnightBoundary(unittest.TestCase):
    """A nightly scheduled just before midnight finishes just after it. Comparing
    raw minute-of-day makes those runs read as ~23h EARLY instead of minutes late."""

    def test_2342_schedule_landing_after_midnight_is_LATE_not_early(self):
        """The reviewer's production-shaped control: talk-events-nightly at 23:42
        with artifacts at 00:05/00:07/00:09 scored deltas of -1417 and returned ok."""
        arts = [("2026-08-16", 5), ("2026-08-17", 7), ("2026-08-18", 9)]
        r = hc._interpret_daily_punctuality(
            [job("talk-events-nightly", 23, 42, arts, today_seen=True)])
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("+25 min late", r["detail"])

    def test_genuinely_early_same_day_artifact_stays_early(self):
        """The guard on the fix: wrapping must not turn an artifact that beat its
        schedule into a next-day run. 23:20 against 23:42 is 22 min EARLY."""
        arts = [(f"2026-08-{d:02d}", 23 * 60 + 20) for d in range(10, 17)]
        r = hc._interpret_daily_punctuality(
            [job("talk-events-nightly", 23, 42, arts, today_seen=True)])
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_after_midnight_schedule_with_previous_day_artifact_stays_early(self):
        """The mirror case: a 00:05 job whose output lands 23:50 the day before is
        early, and must not wrap into +1435 late."""
        arts = [(f"2026-08-{d:02d}", 23 * 60 + 50) for d in range(10, 17)]
        r = hc._interpret_daily_punctuality(
            [job("early-bird", 0, 5, arts, today_seen=True)])
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_ordinary_midday_lateness_is_unchanged(self):
        """The wrap must not perturb the common case it does not apply to."""
        arts = [(f"2026-08-{d:02d}", 6 * 60 + 50 + 40) for d in range(10, 17)]
        r = hc._interpret_daily_punctuality(
            [job("daily-insight", 6, 50, arts, today_seen=True)])
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("+40 min late", r["detail"])


class TestNamingDriftIsUncheckedNotLate(unittest.TestCase):
    """A job whose output filenames changed keeps matching its OLD files forever.
    The probe then reports a lateness median computed from a dead corpus and blames
    the job for 'no output today' — two confident claims about something it stopped
    measuring. Observed live: morning-briefing's output moved from `briefing-<date>`
    to `proactive-morning-<epoch>` on 2026-07-16; for the next 38 days the probe
    reported '7 run(s), median +31 min late' from July files and 'no output today'
    every day, while the job ran fine."""

    def test_stale_corpus_is_unchecked_not_late(self):
        arts = [(f"2026-07-{d:02d}", DUE + 45) for d in range(10, 17)]
        r = hc._interpret_daily_punctuality(
            [job("morning-briefing", 6, 50, arts, today_seen=False, since_due=459)
             | {"naming_stale": True, "newest_artifact": "2026-07-16",
                "artifact_age_days": 39}])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("UNCHECKED", r["detail"])
        self.assertIn("2026-07-16", r["detail"])
        # Assert the RENDERED claim shapes, not bare phrases: the UNCHECKED text
        # quotes these terms while explaining them, so a substring test misfires.
        self.assertNotIn("no output today, 459 min past due", r["detail"])
        self.assertNotIn("run(s), median", r["detail"])

    def test_a_current_corpus_is_still_scored_normally(self):
        """Control: the demotion must be caused by staleness, not by the new field."""
        arts = [(f"2026-08-{d:02d}", DUE + 45) for d in range(10, 17)]
        r = hc._interpret_daily_punctuality(
            [job("daily-insight", 6, 50, arts) | {"naming_stale": False,
                                                  "newest_artifact": "2026-08-16",
                                                  "artifact_age_days": 2}])
        self.assertIn("min late", r["detail"], r)
        self.assertNotIn("UNCHECKED", r["detail"])

    def test_drift_alone_prevents_a_clean_ok(self):
        """Without adding `drifted` to the clean branch, one drifted job among
        otherwise-punctual ones would return ok and certify what nobody measured."""
        good = [(f"2026-08-{d:02d}", DUE + 1) for d in range(10, 17)]
        jobs = [job("fine", 6, 50, good),
                job("drifted", 6, 50, good) | {"naming_stale": True,
                                               "newest_artifact": "2026-06-01",
                                               "artifact_age_days": 84}]
        r = hc._interpret_daily_punctuality(jobs)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("drifted", r["detail"])


class TestCollectorMarksStaleNaming(unittest.TestCase):
    """The collector owns staleness because `now` lives there."""

    def _run(self, newest_day):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td); (ws / "hosts" / "H").mkdir(parents=True)
            (ws / "results").mkdir()
            (ws / "hosts" / "H" / "crons.json").write_text(json.dumps(
                [{"name": "x-insight", "cron": "50 6 * * *"}]))
            (ws / "results" / f"insight-{newest_day}.txt").write_text("x")
            with mock.patch.object(hc, "WORKSPACE_DIR", ws), \
                 mock.patch.object(hc, "_host_label", lambda: "H"):
                return hc.check_daily_cron_punctuality()

    def test_old_only_artifact_is_marked_stale(self):
        r = self._run("2026-01-05")
        self.assertIn("UNCHECKED", r["detail"], r)
        self.assertIn("2026-01-05", r["detail"])

    def test_todays_artifact_is_not_marked_stale(self):
        r = self._run(datetime.now().strftime("%Y-%m-%d"))
        self.assertNotIn("UNCHECKED", r["detail"], r)

    def test_a_shape_valid_but_impossible_date_does_not_crash_the_probe(self):
        """`_daily_artifact_minutes` matches dates by SHAPE (\d{4}-\d{2}-\d{2}),
        so `2026-13-45` reaches the parser and raises. An unparseable date makes
        the age unknown, which must read as "cannot tell", never as stale."""
        r = self._run("2026-13-45")
        self.assertNotIn("UNCHECKED", r["detail"], r)
        self.assertIn(r["status"], ("ok", "warn"))



class ArtifactDateSpellingTests(unittest.TestCase):
    """`<stem>-YYYYMMDD` and `<stem>-YYYY-MM-DD` are both in live use, and the
    compact form is the majority; matching only the hyphenated one reports a
    job that writes an artifact every day as having produced nothing."""

    def _minutes(self, names):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            for n in names:
                (r / n).write_text("x")
            return hc._daily_artifact_minutes(r, "widget-report")

    def test_compact_dates_are_found(self):
        got = self._minutes(["widget-report-20260825.txt", "widget-report-20260826.txt"])
        self.assertEqual([d for d, _ in got], ["2026-08-25", "2026-08-26"],
                         "compact YYYYMMDD artifacts must be seen, and normalised to ISO")

    def test_hyphenated_dates_still_work(self):
        got = self._minutes(["widget-report-2026-08-25.txt"])
        self.assertEqual([d for d, _ in got], ["2026-08-25"])

    def test_both_spellings_coexist_in_one_directory(self):
        got = self._minutes(["widget-report-20260824.txt", "widget-report-2026-08-25.txt"])
        self.assertEqual([d for d, _ in got], ["2026-08-24", "2026-08-25"],
                         "a directory carrying both conventions must yield both")

    def test_a_non_date_suffix_is_still_rejected(self):
        # The control that can fail: widening the date match must not turn the
        # matcher into a prefix match on the stem.
        self.assertEqual(self._minutes(["widget-report-2026notadate.txt"]), [])
        self.assertEqual(self._minutes(["widget-report-summary.txt"]), [])

    def test_the_returned_date_parses_as_the_caller_expects(self):
        # The caller does strptime(newest, "%Y-%m-%d") and compares against
        # now.strftime("%Y-%m-%d"); a raw 20260826 would raise there.
        (d, _), = self._minutes(["widget-report-20260826.txt"])
        datetime.strptime(d, "%Y-%m-%d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
