#!/usr/bin/env python3
"""scheduled-panel/publish_schedule.py renders and publishes; every schedule
judgement (cron semantics, per-job zone, file shape, last-fire record) is
delegated to src/dashboard_schedules, so the panel cannot drift from the
schedulers. Cron semantics themselves are pinned in tests/cron-eval.test.py.

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


class HumanCronTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ps._human_cron("*/5 * * * *"), "every 5 min")
        self.assertEqual(ps._human_cron("1,31 * * * *"), "~every few min (1,31)")
        self.assertEqual(ps._human_cron("7 16 * * *"), "daily 16:07")
        self.assertEqual(ps._human_cron("7 16 * * 5"), "5 16:07")
        self.assertEqual(ps._human_cron("4 6 */3 * *"), "4 6 */3 * *")  # honest fallback
        self.assertEqual(ps._human_cron("bad"), "bad")

    def test_a_calendar_restriction_is_never_hidden(self):
        # "every 5 min" would be false 30 days of 31: the restriction stays visible.
        self.assertEqual(ps._human_cron("*/5 * 1 * *"), "*/5 * 1 * *")
        self.assertEqual(ps._human_cron("1,31 * * 9 *"), "1,31 * * 9 *")
        self.assertEqual(ps._human_cron("*/5 * * * 1"), "*/5 * * * 1")


class FmtTests(unittest.TestCase):
    def test_aware_to_utc_minute(self):
        tokyo = datetime.timezone(datetime.timedelta(hours=9))
        self.assertEqual(ps._fmt(datetime.datetime(2026, 9, 3, 21, 0, tzinfo=tokyo)), "2026-09-03T12:00Z")

    def test_none_and_junk_are_a_dash(self):
        self.assertEqual(ps._fmt(None), "—")
        self.assertEqual(ps._fmt("2026-09-03 00:40:11"), "—")

    def test_naive_is_taken_as_host_local(self):
        naive = datetime.datetime(2026, 9, 3, 12, 0)
        self.assertEqual(ps._fmt(naive), naive.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"))


class CfgTests(unittest.TestCase):
    def test_reads_the_repo_config_helper(self):
        self.assertTrue(ps._cfg("workspace"))

    def test_a_failing_config_helper_is_unavailable_not_empty(self):
        fake = types.SimpleNamespace(returncode=3, stdout="", stderr="sutando config: malformed")
        with mock.patch.object(ps.subprocess, "run", return_value=fake):
            with self.assertRaises(ps.SourceUnavailable):
                ps._cfg("workspace")
        fake = types.SimpleNamespace(returncode=0, stdout="\n", stderr="")
        with mock.patch.object(ps.subprocess, "run", return_value=fake):
            with self.assertRaises(ps.SourceUnavailable):
                ps._cfg("workspace")


class StrictSourceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.p = pathlib.Path(self.td.name) / "crons.json"

    def test_missing_invalid_and_wrong_shape_are_unavailable(self):
        with self.assertRaises(ps.SourceUnavailable):
            ps.read_crons_strict(self.p)
        self.p.write_text("{not json")
        with self.assertRaises(ps.SourceUnavailable):
            ps.read_crons_strict(self.p)
        self.p.write_text(json.dumps({"jobs": []}))
        with self.assertRaises(ps.SourceUnavailable):
            ps.read_crons_strict(self.p)

    def test_a_valid_empty_list_is_an_empty_schedule(self):
        self.p.write_text("[]")
        self.assertEqual(ps.read_crons_strict(self.p), [])

    def test_main_refuses_to_publish_on_an_unavailable_source(self):
        calls = []
        with mock.patch.object(ps, "build_rows", side_effect=ps.SourceUnavailable("cfg rc=3")), \
             mock.patch.object(ps, "publish", side_effect=lambda *a, **k: calls.append(a) or {"ok": True}):
            rc = ps.main(["--publish", "--room", "!r:x"])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [], "doc_put ran on an unavailable source")

    def test_main_other_paths(self):
        with mock.patch.object(ps, "build_rows", return_value=[]):
            self.assertEqual(ps.main(["--json"]), 0)
            self.assertEqual(ps.main([]), 0)
            with mock.patch.object(ps, "publish", return_value={"ok": False, "reason": "boom"}):
                self.assertEqual(ps.main(["--publish", "--room", "!r:x"]), 1)

    def test_main_publishes_a_valid_empty_schedule(self):
        calls = []
        with mock.patch.object(ps, "build_rows", return_value=[]), \
             mock.patch.object(ps, "publish", side_effect=lambda room, md: calls.append((room, md)) or {"ok": True}):
            rc = ps.main(["--publish", "--room", "!r:x"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("# Scheduled jobs", calls[0][1])


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = self.td.name
        self.state = os.path.join(self.ws, "state")
        os.makedirs(os.path.join(self.state, "schedules"))
        os.makedirs(os.path.join(self.ws, "hosts", "h1"))

    def _write(self, rel, obj):
        with open(os.path.join(self.ws, rel), "w") as fh:
            json.dump(obj, fh)

    def _rows(self, jobs, now=NOW):
        self._write("hosts/h1/crons.json", jobs)
        with mock.patch.object(ps, "_cfg", side_effect=lambda a: {"workspace": self.ws, "host-label": "h1"}[a]):
            return ps.build_rows(now)


class BuildRowsTests(_Workspace):
    def test_rows_from_the_canonical_list_shape(self):
        rows = self._rows([
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "dyn", "loop": "dynamic", "cron": "", "prompt": "x" * 60},
            {"cron": "7 16 * * 5"},
            "not a job",
        ])
        self.assertEqual([r["name"] for r in rows], ["main-loop", "dyn", "?"])
        self.assertEqual(rows[0]["schedule"], "every 5 min")
        self.assertEqual(rows[0]["owner"], "session")
        self.assertEqual(rows[0]["does"], "proactive-loop")
        self.assertEqual(rows[0]["next_fire"], "2026-09-03T00:45Z")
        self.assertEqual(rows[1]["schedule"], "adaptive (self-paced)")
        self.assertEqual(rows[1]["next_fire"], "—")
        self.assertEqual(rows[1]["does"], "x" * 48)
        self.assertEqual(rows[2]["does"], "—")

    def test_dict_shape_is_unavailable_not_an_empty_schedule(self):
        # The schedulers reject a non-array crons.json; the panel must neither
        # publish jobs no scheduler owns nor overwrite the doc with an empty table.
        with self.assertRaises(ps.SourceUnavailable):
            self._rows({"jobs": [{"name": "a", "cron": "0 0 * * *"}]})
        with self.assertRaises(ps.SourceUnavailable):
            self._rows({"crons": [{"name": "b", "cron": "0 0 * * *"}]})

    def test_codex_job_next_fire_is_in_its_own_zone(self):
        rows = self._rows([{"name": "d", "cron": "0 6 * * *", "execution": "codex-task", "prompt": "x"},
                           {"name": "t", "cron": "0 6 * * *", "execution": "codex-task", "prompt": "x",
                            "timezone": "Asia/Tokyo"}])
        self.assertEqual(rows[0]["next_fire"], "2026-09-03T13:00Z")  # 06:00 Los Angeles
        self.assertEqual(rows[1]["next_fire"], "2026-09-03T21:00Z")  # 06:00 Tokyo
        self.assertEqual(rows[0]["owner"], "codex")

    def test_sunday_seven_and_leap_day_have_a_next_fire(self):
        rows = self._rows([{"name": "sun", "cron": "0 6 * * 7", "execution": "codex-task", "prompt": "x"},
                           {"name": "leap", "cron": "0 0 29 2 *", "execution": "codex-task", "prompt": "x"},
                           {"name": "never", "cron": "0 0 31 2 *", "execution": "codex-task", "prompt": "x"}])
        self.assertEqual(rows[0]["next_fire"], "2026-09-06T13:00Z")
        self.assertEqual(rows[1]["next_fire"], "2028-02-29T08:00Z")
        self.assertEqual(rows[2]["next_fire"], "—")

    def test_last_fired_comes_from_the_owning_scheduler_only(self):
        self._write("state/core-status.json", {"ts": "2026-09-03 12:34:00"})
        self._write("state/schedules/codex-scheduler.json",
                    {"jobs": {"nightly": {"last_scheduled_slot": "2026-09-02T13:00:00Z"}}})
        # boundary (moves every tick) vs the fire record: only the record renders
        self._write("state/cron-runner-state.json",
                    {"digest": 1788393600, "__fired__": {"digest": 1788390000}})
        self._write("state/ghost.json", {"last_pass": 1})
        self._write("outside.json", {"last_pass": 1})
        rows = self._rows([
            {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "nightly", "cron": "0 6 * * *", "execution": "codex-task", "prompt": "x"},
            {"name": "digest", "cron": "0 6 * * *", "launchd": True},
            {"name": "ghost", "cron": "0 6 * * *"},
            {"name": "../outside", "cron": "0 6 * * *", "launchd": True},
        ])
        by = {r["name"]: r["last_fired"] for r in rows}
        self.assertEqual(by["main-loop"], "—")           # core-status.ts is activity, not a fire
        self.assertEqual(by["nightly"], "2026-09-02T13:00Z")
        self.assertEqual(by["digest"], "2026-09-02T23:00Z")
        self.assertEqual(by["ghost"], "—")               # no <name>.json convention
        self.assertEqual(by["../outside"], "—")          # no path escape


class RenderTests(unittest.TestCase):
    def test_markdown_table(self):
        md = ps.render_md([{"name": "a", "schedule": "s", "owner": "o", "last_fired": "l",
                            "next_fire": "n", "does": "d"}])
        self.assertTrue(md.startswith("# Scheduled jobs\n*updated "))
        self.assertIn("source: durable crons.json", md)
        self.assertIn("| Job | Schedule | Fires via | Last fired | Next fire | Does |", md)
        self.assertTrue(md.endswith("| a | s | o | l | n | d |\n"))


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
    unittest.main(verbosity=1)
