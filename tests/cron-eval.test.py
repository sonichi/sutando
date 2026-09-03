#!/usr/bin/env python3
"""src/cron_eval.py is the one cron evaluation contract, and the launchd runner,
the codex scheduler, dashboard_schedules and the scheduled-panel all bind it.

Contract: grammar, Sunday=7 (incl. ranges 5-7 / 0-7), Vixie dom/dow OR, leap
day, never-fires, malformed raises, clamp semantics, per-job timezone.
Wiring: each caller is proven to reach cron_eval by making cron_eval raise a
sentinel and watching it surface through the caller's public function.
Run: python3 tests/cron-eval.test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import cron_eval as ce  # noqa: E402
import dashboard_schedules as ds  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load("cron_runner", REPO / "src" / "cron-runner.py")
cx = _load("codex_scheduler", REPO / "skills" / "schedule-crons" / "scripts" / "codex-scheduler.py")

NOW = datetime(2026, 9, 3, 0, 40)  # a Thursday, naive wall clock


class Sentinel(RuntimeError):  # not ValueError: the wrappers may swallow that on purpose
    pass


class ContractTests(unittest.TestCase):
    def test_grammar(self):
        self.assertEqual(ce.field_values("*", 0, 3), {0, 1, 2, 3})
        self.assertEqual(ce.field_values("*/20", 0, 59), {0, 20, 40})
        self.assertEqual(ce.field_values("1,5", 0, 6), {1, 5})
        self.assertEqual(ce.field_values("2-4", 0, 6), {2, 3, 4})
        self.assertEqual(ce.field_values("1-10/3", 1, 31), {1, 4, 7, 10})
        self.assertEqual(ce.field_values("*/3", 1, 31), set(range(1, 32, 3)))  # lo-based, not value%3

    def test_malformed_raises(self):
        for bad in ("", "*/0", "9-3", "60", "x", "1-x", "*/x"):
            with self.assertRaises(ValueError, msg=bad):
                ce.field_values(bad, 0, 59)
        with self.assertRaises(ValueError):
            ce.parse("* * * *")

    def test_clamp_is_the_launchd_contract(self):
        self.assertEqual(max(ce.field_values("0-70", 0, 59, clamp=True)), 59)
        self.assertEqual(ce.field_values("5-3", 0, 59, clamp=True), frozenset())
        with self.assertRaises(ValueError):
            ce.field_values("x", 0, 59, clamp=True)  # syntax still raises

    def test_sunday_seven_and_ranges(self):
        self.assertEqual(ce.field_values("7", 0, 7, sunday_7=True), {0})
        self.assertEqual(ce.field_values("5-7", 0, 7, sunday_7=True), {0, 5, 6})
        self.assertEqual(ce.field_values("0-7", 0, 7, sunday_7=True), set(range(7)))
        self.assertEqual(ce.next_match("0 6 * * 7", NOW, 8), datetime(2026, 9, 6, 6, 0))  # Sunday
        self.assertTrue(ce.matches("0 6 * * 5-7", datetime(2026, 7, 5, 6, 0)))   # Sun
        self.assertFalse(ce.matches("0 6 * * 5-6", datetime(2026, 7, 5, 6, 0)))

    def test_vixie_or_when_both_restricted(self):
        self.assertEqual(ce.next_match("0 12 15 * 1", NOW, 8), datetime(2026, 9, 7, 12, 0))  # Monday first
        self.assertEqual(ce.next_match("0 12 * * 1", NOW, 8), datetime(2026, 9, 7, 12, 0))
        self.assertEqual(ce.next_match("0 12 15 * *", NOW, 30), datetime(2026, 9, 15, 12, 0))

    def test_leap_day_and_never(self):
        self.assertEqual(ce.next_match("0 0 29 2 *", NOW, 366 * 4 + 1), datetime(2028, 2, 29, 0, 0))
        self.assertIsNone(ce.next_match("0 0 31 2 *", NOW, 366 * 4 + 1))
        self.assertIsNone(ce.next_match("0 0 29 2 *", NOW, 400))  # a short horizon honestly says none

    def test_day_of_month_step_and_annual(self):
        self.assertEqual(ce.next_match("4 6 */3 * *", NOW, 8), datetime(2026, 9, 4, 6, 4))
        self.assertEqual(ce.next_match("23 9 7 8 *", NOW, 400), datetime(2027, 8, 7, 9, 23))
        self.assertEqual(ce.next_match("40 * * * *", NOW, 1), datetime(2026, 9, 3, 1, 40))


class PerJobTimezoneTests(unittest.TestCase):
    NOW_UTC = datetime(2026, 9, 3, 0, 40, tzinfo=timezone.utc)

    def test_codex_job_fires_in_its_declared_zone(self):
        job = {"name": "daily", "cron": "0 6 * * *", "execution": "codex-task", "prompt": "x"}
        nxt = ds.next_run_for_job(job, self.NOW_UTC)
        self.assertEqual(nxt.astimezone(timezone.utc), datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc))
        job["timezone"] = "Asia/Tokyo"
        nxt = ds.next_run_for_job(job, self.NOW_UTC)
        self.assertEqual(nxt.astimezone(timezone.utc), datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc))

    def test_unknown_zone_is_none_not_a_crash(self):
        job = {"name": "z", "cron": "0 6 * * *", "execution": "codex-task", "timezone": "Mars/Olympus"}
        self.assertIsNone(ds.next_run_for_job(job, self.NOW_UTC))

    def test_host_local_job_evaluates_on_host_wall_clock(self):
        job = {"name": "s", "cron": "0 6 * * *", "launchd": True}
        local = self.NOW_UTC.astimezone()
        want = ce.next_match("0 6 * * *", local, 8)
        self.assertEqual(ds.next_run_for_job(job, self.NOW_UTC), want)

    def test_dynamic_loop_and_invalid_are_none(self):
        self.assertIsNone(ds.next_run_for_job({"name": "d", "loop": "dynamic"}, self.NOW_UTC))
        self.assertIsNone(ds.next_run_for_job({"name": "b", "cron": "x * * * *"}, self.NOW_UTC))


class LastFiredTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = Path(self.td.name)
        (self.ws / "state" / "schedules").mkdir(parents=True)
        self.state = self.ws / "state"

    def test_main_loop_never_reads_core_status(self):
        (self.state / "core-status.json").write_text(json.dumps({"ts": "2026-09-03 12:34:00", "status": "running"}))
        self.assertIsNone(ds.last_fired({"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"}, self.state))

    def test_codex_record(self):
        (self.state / "schedules" / "codex-scheduler.json").write_text(json.dumps(
            {"jobs": {"nightly": {"last_scheduled_slot": "2026-09-02T13:00:00Z"}}}))
        got = ds.last_fired({"name": "nightly", "cron": "0 6 * * *", "execution": "codex-task"}, self.state)
        self.assertEqual(got, datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc))

    def test_launchd_record(self):
        (self.state / "cron-runner-state.json").write_text(json.dumps({"digest": 1788390000}))
        got = ds.last_fired({"name": "digest", "cron": "0 6 * * *", "launchd": True}, self.state)
        self.assertEqual(got, datetime.fromtimestamp(1788390000, timezone.utc))

    def test_dynamic_alive_mtime(self):
        p = self.state / "dynamic-loop-loopy.alive"
        p.write_text("")
        os.utime(p, (0, 0))
        got = ds.last_fired({"name": "loopy", "loop": "dynamic"}, self.state)
        self.assertEqual(got, datetime.fromtimestamp(0, timezone.utc))

    def test_job_name_cannot_escape_state(self):
        (self.ws / "outside.json").write_text(json.dumps({"last_pass": 1}))
        (self.ws / "dynamic-loop-..").mkdir(exist_ok=True)
        for name in ("../outside", "../../etc/passwd", "", "a/b"):
            self.assertIsNone(ds.last_fired({"name": name, "cron": "0 0 * * *", "launchd": True}, self.state), name)
            self.assertIsNone(ds.last_fired({"name": name, "loop": "dynamic"}, self.state), name)

    def test_session_cron_has_no_record(self):
        self.assertIsNone(ds.last_fired({"name": "x", "cron": "0 0 * * *"}, self.state))


class WiringTests(unittest.TestCase):
    """Each production caller reaches cron_eval: a sentinel raised inside
    cron_eval surfaces through the caller's own public function."""

    def test_launchd_runner_binds_cron_eval(self):
        with mock.patch.object(ce, "field_values", side_effect=Sentinel("fv")):
            cr._parse_field.cache_clear()
            with self.assertRaises(Sentinel):
                cr.cron_matches("0 6 * * *", time.localtime())
        cr._parse_field.cache_clear()
        with mock.patch.object(ce.CronSpec, "date_matches", side_effect=Sentinel("dm")):
            with self.assertRaises(Sentinel):
                cr._day_matches("*", "*", "7", time.localtime())
        cr._parse_field.cache_clear()
        self.assertTrue(cr.cron_matches("0 6 * * 7", time.struct_time((2026, 7, 5, 6, 0, 0, 6, 186, 0))))

    def test_codex_scheduler_binds_cron_eval(self):
        with mock.patch.object(ce, "matches", side_effect=Sentinel("m")):
            with self.assertRaises(Sentinel):
                cx.cron_matches("0 6 * * *", datetime(2026, 1, 1, tzinfo=timezone.utc))
        with mock.patch.object(ce, "field_values", side_effect=Sentinel("fv")):
            with self.assertRaises(Sentinel):
                cx._field_values("*", 0, 59)
        self.assertEqual(cx._field_values("7", 0, 7, sunday_7=True), {0})

    def test_dashboard_schedules_binds_cron_eval(self):
        with mock.patch.object(ce, "next_match", side_effect=Sentinel("nm")):
            with self.assertRaises(Sentinel):
                ds.next_run("0 6 * * *", NOW)
        self.assertEqual(ds.next_run("0 12 15 * 1", NOW), datetime(2026, 9, 7, 12, 0))  # OR, via cron_eval
        self.assertTrue(ds.cron_field_match("7", 0, 0, 7))   # Sunday=7 through the legacy helper
        self.assertTrue(ds.cron_field_match("*/5", 15))
        self.assertFalse(ds.cron_field_match("foo", 7))

    def test_all_three_agree_on_the_reviewer_probes(self):
        la = ZoneInfo("America/Los_Angeles")
        for expr, at in (("0 6 * * *", datetime(2026, 9, 3, 6, 0)),
                         ("0 6 * * 7", datetime(2026, 9, 6, 6, 0)),
                         ("0 0 29 2 *", datetime(2028, 2, 29, 0, 0)),
                         ("*/5 * 1 * *", datetime(2026, 10, 1, 0, 5))):
            self.assertTrue(cx.cron_matches(expr, at.replace(tzinfo=la)), expr)
            self.assertTrue(cr.cron_matches(expr, at.timetuple()), expr)
            self.assertEqual(ds.next_run(expr, at - timedelta(minutes=1), 1), at, expr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
