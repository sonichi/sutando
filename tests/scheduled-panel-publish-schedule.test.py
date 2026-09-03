#!/usr/bin/env python3
"""scheduled-panel/publish_schedule.py — the next-fire column must honour every
cron field. Measured on a live crons.json: `4 6 */3 * *` and `23 9 7 8 *`
(annual) both rendered as "fires today" because day-of-month and month were
never consulted, beside a human column that fell back honestly to the raw
expression for exactly those fields.

Hermetic: `_cfg` is patched to a temp workspace; publish() gets a stub doc
module. Run: python3 tests/scheduled-panel-publish-schedule.test.py
"""
import datetime
import importlib.util
import json
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock

_SRC = pathlib.Path(__file__).resolve().parents[1] / "skills" / "scheduled-panel" / "publish_schedule.py"
_spec = importlib.util.spec_from_file_location("publish_schedule", _SRC)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 3, 0, 40, tzinfo=UTC)  # a Thursday


class FieldSetTests(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(ps._field_set("*", 0, 3), {0, 1, 2, 3})
        self.assertEqual(ps._field_set("*/20", 0, 59), {0, 20, 40})
        self.assertEqual(ps._field_set("1,5", 0, 6), {1, 5})
        self.assertEqual(ps._field_set("2-4", 0, 6), {2, 3, 4})
        self.assertEqual(ps._field_set("1-10/3", 1, 31), {1, 4, 7, 10})
        self.assertEqual(ps._field_set("*/3", 1, 31), set(range(1, 32, 3)))


class HumanCronTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ps._human_cron("*/5 * * * *"), "every 5 min")
        self.assertEqual(ps._human_cron("1,31 * * * *"), "~every few min (1,31)")
        self.assertEqual(ps._human_cron("7 16 * * *"), "daily 16:07")
        self.assertEqual(ps._human_cron("7 16 * * 5"), "5 16:07")
        self.assertEqual(ps._human_cron("4 6 */3 * *"), "4 6 */3 * *")  # honest fallback
        self.assertEqual(ps._human_cron("bad"), "bad")


class NextFireTests(unittest.TestCase):
    """Reviewer's four live expressions at now=2026-09-03T00:40Z, two of them controls."""

    def test_day_of_month_step_is_consulted(self):
        # */3 -> days 1,4,7,...; the 3rd is not one, so NOT 2026-09-03T06:04Z.
        self.assertEqual(ps._next_fire("4 6 */3 * *", NOW), "2026-09-04T06:04Z")

    def test_annual_job_is_eleven_months_out_not_nine_hours(self):
        self.assertEqual(ps._next_fire("23 9 7 8 *", NOW), "2027-08-07T09:23Z")

    def test_controls_unchanged(self):
        self.assertEqual(ps._next_fire("*/5 * * * *", NOW), "2026-09-03T00:45Z")
        self.assertEqual(ps._next_fire("7 16 * * 5", NOW), "2026-09-04T16:07Z")  # Friday

    def test_month_field(self):
        self.assertEqual(ps._next_fire("0 12 * 10 *", NOW), "2026-10-01T12:00Z")

    def test_dom_and_dow_both_restricted_are_or_ed(self):
        # Vixie cron: the 15th OR a Monday. Mon 2026-09-07 comes before the 15th.
        self.assertEqual(ps._next_fire("0 12 15 * 1", NOW), "2026-09-07T12:00Z")
        # Only dow restricted: still AND with the wildcard dom -> the same Monday.
        self.assertEqual(ps._next_fire("0 12 * * 1", NOW), "2026-09-07T12:00Z")

    def test_never_fires_and_malformed(self):
        self.assertEqual(ps._next_fire("0 0 31 2 *", NOW), "—")  # Feb 31 never comes
        self.assertEqual(ps._next_fire("not a cron", NOW), "—")
        self.assertEqual(ps._next_fire("x * * * *", NOW), "—")

    def test_next_minute_boundary(self):
        # Fires at :40 exactly when now is :40 -> the scan starts at :41, so next hour.
        self.assertEqual(ps._next_fire("40 * * * *", NOW), "2026-09-03T01:40Z")


class IsoTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ps._iso(0), "1970-01-01T00:00Z")
        self.assertEqual(ps._iso("2026-09-03 00:40:11"), "2026-09-03T00:40")
        self.assertEqual(ps._iso(None), "None")

    def test_unrenderable_is_a_dash(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("no")
        self.assertEqual(ps._iso(Boom()), "—")


class CfgTests(unittest.TestCase):
    def test_reads_the_repo_config_helper(self):
        # The real helper, in-repo: the path it prints is a directory name, never empty.
        self.assertTrue(ps._cfg("workspace"))


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = self.td.name
        self.state = os.path.join(self.ws, "state")
        os.makedirs(self.state)
        os.makedirs(os.path.join(self.ws, "hosts", "h1"))

    def _write(self, rel, obj):
        with open(os.path.join(self.ws, rel), "w") as fh:
            json.dump(obj, fh)


class LastFiredTests(_Workspace):
    def test_main_loop_reads_core_status(self):
        self._write("state/core-status.json", {"ts": "2026-09-03 00:30:00"})
        self.assertEqual(ps._last_fired({"name": "main-loop"}, self.ws, "h1"), "2026-09-03T00:30")

    def test_per_cron_state_last_pass(self):
        self._write("state/shepherd.json", {"last_pass": 1})
        self.assertEqual(ps._last_fired({"name": "shepherd"}, self.ws, "h1"), "1970-01-01T00:00Z")

    def test_main_loop_without_a_stamp_falls_through(self):
        self._write("state/core-status.json", {"status": "idle"})
        self.assertEqual(ps._last_fired({"name": "main-loop"}, self.ws, "h1"), "—")

    def test_alive_file_mtime(self):
        p = os.path.join(self.state, "dynamic-loop-loopy.alive")
        open(p, "w").close()
        os.utime(p, (0, 0))
        self.assertEqual(ps._last_fired({"name": "loopy"}, self.ws, "h1"), "1970-01-01T00:00Z")

    def test_nothing_known(self):
        self.assertEqual(ps._last_fired({"name": "ghost"}, self.ws, "h1"), "—")


class BuildRowsTests(_Workspace):
    def _rows(self, jobs):
        self._write("hosts/h1/crons.json", jobs)
        with mock.patch.object(ps, "_cfg", side_effect=lambda a: {"workspace": self.ws, "host-label": "h1"}[a]):
            return ps.build_rows()

    def test_rows_from_list_shape(self):
        rows = self._rows([
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "dyn", "loop": "dynamic", "cron": "", "prompt": "x" * 60},
            {"cron": "7 16 * * 5"},
        ])
        self.assertEqual([r["name"] for r in rows], ["main-loop", "dyn", "?"])
        self.assertEqual(rows[0]["schedule"], "every 5 min")
        self.assertEqual(rows[0]["does"], "proactive-loop")
        self.assertRegex(rows[0]["next_fire"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")
        self.assertEqual(rows[1]["schedule"], "adaptive (self-paced)")
        self.assertEqual(rows[1]["next_fire"], "—")
        self.assertEqual(rows[1]["does"], "x" * 48)
        self.assertEqual(rows[2]["does"], "—")

    def test_rows_from_dict_shape(self):
        rows = self._rows({"jobs": [{"name": "a", "cron": "0 0 * * *"}]})
        self.assertEqual(rows[0]["schedule"], "daily 00:00")
        rows = self._rows({"crons": [{"name": "b", "cron": "0 0 * * *"}]})
        self.assertEqual(rows[0]["name"], "b")


class RenderTests(unittest.TestCase):
    def test_markdown_table(self):
        md = ps.render_md([{"name": "a", "schedule": "s", "last_fired": "l", "next_fire": "n", "does": "d"}])
        self.assertTrue(md.startswith("# Scheduled jobs\n*updated "))
        self.assertIn("source: durable crons.json", md)
        self.assertIn("| Job | Schedule | Last fired | Next fire | Does |", md)
        self.assertTrue(md.endswith("| a | s | l | n | d |\n"))


class PublishTests(unittest.TestCase):
    def test_delegates_to_room_ops_doc_put(self):
        calls = []

        class _Loader:
            def exec_module(self, mod):
                mod.doc_put = lambda *a, **k: (calls.append((a, k)), {"ok": True})[1]

        fake = types.SimpleNamespace(loader=_Loader())
        with mock.patch.object(ps.importlib.util, "spec_from_file_location", return_value=fake), \
             mock.patch.object(ps.importlib.util, "module_from_spec", side_effect=lambda s: types.ModuleType("_ops_doc")):
            res = ps.publish("!room:hs", "# body")
        self.assertEqual(res, {"ok": True})
        (args, kw), = calls
        self.assertEqual(args, ("!room:hs", "# body"))
        self.assertEqual(kw, {"folder": "activity", "name": "SCHEDULE.md", "message": "scheduled-jobs refresh"})
        self.assertIn(os.path.join(ps.REPO, "skills/agent-room-ops"), ps.sys.path)


class ArgTests(unittest.TestCase):
    def test_flag_value(self):
        with mock.patch.object(ps.sys, "argv", ["x", "--room", "!r:hs"]):
            self.assertEqual(ps._arg("--room"), "!r:hs")
            self.assertIsNone(ps._arg("--nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
