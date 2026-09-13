#!/usr/bin/env python3
"""The picker shows what we advertise, so the two bodies must agree.

Status comes from the snapshot, the name from the profile patch. A worker in
one and not the other renders wrong rather than absent, which is why both are
built from a single roster read.

Run: python3 tests/pool-advertise.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(Path(__file__).resolve().parents[3] / "src"))

import pool_advertise as pa  # noqa: E402

import pool_roster as pr  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
W3 = "c5f13e4d6a7b8c9d0e1f2a3b4c5d6e7f"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)

    def roster(self, workers):
        return pr.compile_roster(self.ws, workers, {})


class TestStatusComesFromState(Base):
    def test_live_is_active_and_abandoned_is_dead(self):
        r = self.roster({W1: {"state": "live"}, W2: {"state": "abandoned"}})
        snap = pa.snapshot(r, now=1000)
        self.assertEqual(snap["live_cores"], [W1])
        self.assertEqual(snap["dead_cores"], [W2])
        self.assertEqual(snap["ts"], 1000)

    def test_recovering_is_not_answering_never_available(self):
        r = self.roster({W1: {"state": "recovering"}})
        snap = pa.snapshot(r, now=1)
        self.assertEqual(snap["live_cores"], [])
        self.assertEqual(snap["dead_cores"], [W1])  # not answering now; never "available"

    def test_the_roster_version_rides_along(self):
        r = self.roster({W1: {"state": "live"}})
        self.assertEqual(pa.snapshot(r, now=1)["roster_version"], r["version"])


class TestNamesComeFromTheProfilePatch(Base):
    def test_a_label_is_carried(self):
        r = self.roster({W1: {"state": "live", "label": "reviewer"}})
        self.assertEqual(pa.profile_workers(r)[W1]["label"], "reviewer")

    def test_an_unlabelled_worker_falls_back_to_its_id(self):
        r = self.roster({W1: {"state": "live"}})
        self.assertEqual(pa.profile_workers(r)[W1]["label"], W1)

    def test_a_declared_runtime_is_carried_so_a_codex_worker_reads_right(self):
        r = self.roster({W1: {"state": "live", "runtime": "codex"}})
        self.assertEqual(pa.profile_workers(r)[W1]["runtime"], "codex")

    def test_no_runtime_is_omitted_rather_than_guessed(self):
        # The broker defaults an absent runtime to claude; sending one we did
        # not declare would make a guess look like a declaration.
        r = self.roster({W1: {"state": "live"}})
        self.assertNotIn("runtime", pa.profile_workers(r)[W1])

    def test_a_retired_worker_is_advertised_nowhere(self):
        # The broker renders any id it has metadata for, so leaving a retired
        # worker here would pin a deleted worker to the picker permanently.
        r = self.roster({W1: {"state": "live"}, W2: {"state": "retired"}})
        self.assertNotIn(W2, pa.profile_workers(r))
        snap = pa.snapshot(r, now=1)
        self.assertNotIn(W2, snap["live_cores"] + snap["dead_cores"])


class TestTheTwoBodiesAgree(Base):
    def test_every_advertised_status_has_a_name(self):
        r = self.roster({W1: {"state": "live", "label": "one"},
                         W2: {"state": "abandoned"},
                         W3: {"state": "recovering", "label": "three"}})
        ad = pa.advertisement(self.ws, now=1)
        named = set(ad["profile_patch"]["workers"])
        snap = ad["workers_snapshot"]
        self.assertTrue(set(snap["live_cores"] + snap["dead_cores"]) <= named)

    def test_both_bodies_come_from_one_roster_read(self):
        self.roster({W1: {"state": "live", "label": "one"}})
        ad = pa.advertisement(self.ws, now=7)
        self.assertEqual(ad["workers_snapshot"]["live_cores"], [W1])
        self.assertEqual(ad["profile_patch"]["workers"][W1]["label"], "one")

    def test_no_roster_refuses_rather_than_advertising_an_empty_pool(self):
        # An empty advertisement would retire every worker the picker shows.
        with self.assertRaises(FileNotFoundError):
            pa.advertisement(self.ws)


class TestCli(Base):
    def test_it_prints_both_bodies_as_json(self):
        self.roster({W1: {"state": "live", "label": "one"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pa.main(["--workspace", str(self.ws)]), 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got["workers_snapshot"]["live_cores"], [W1])
        self.assertEqual(got["profile_patch"]["workers"][W1]["label"], "one")

    def test_no_roster_exits_two_and_says_so(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(pa.main(["--workspace", str(self.ws)]), 2)
        self.assertIn("no roster", err.getvalue())


class AdvertisementFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        pr.compile_roster(self.ws, {"w1": {"label": "alpha", "state": "live",
                                            "runtime": "claude"}}, {}, version=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_file_carries_both_bodies_exactly(self):
        path = pa.write_advertisement(self.ws, now=1700000000)
        self.assertEqual(path, self.ws / "state" / "pool-advertisement.json")
        got = json.loads(path.read_text())
        ad = pa.advertisement(self.ws, now=1700000000)
        self.assertEqual(got["ts"], 1700000000)
        self.assertEqual(got["workers"], ad["workers_snapshot"])
        self.assertEqual(got["profile_workers"], ad["profile_patch"]["workers"])
        self.assertEqual(got["profile_workers"]["w1"]["label"], "alpha")

    def test_the_write_leaves_no_temp_file_behind(self):
        pa.write_advertisement(self.ws)
        names = sorted(p.name for p in (self.ws / "state").iterdir())
        self.assertNotIn(True, [n.startswith(".pool-advertisement.") for n in names])
        self.assertIn("pool-advertisement.json", names)

    def test_the_cli_write_flag_writes_and_prints_the_path(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pa.main(["--workspace", str(self.ws), "--write"])
        self.assertEqual(rc, 0)
        self.assertIn("pool-advertisement.json", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["profile_patch"]["workers"]["w1"]["label"], "alpha")
        self.assertTrue((self.ws / "state" / "pool-advertisement.json").exists())

    def test_a_failed_replace_leaves_neither_file_nor_temp(self):
        import os
        real = os.replace

        def boom(src, dst):
            raise OSError("disk full")

        pa.os.replace = boom
        try:
            with self.assertRaises(OSError):
                pa.write_advertisement(self.ws)
        finally:
            pa.os.replace = real
        names = [p.name for p in (self.ws / "state").iterdir()]
        self.assertNotIn("pool-advertisement.json", names)
        self.assertFalse(any(n.startswith(".pool-advertisement.") for n in names))

    def test_no_roster_means_no_file(self):
        empty = Path(tempfile.mkdtemp())
        with self.assertRaises(FileNotFoundError):
            pa.write_advertisement(empty)
        self.assertFalse((empty / "state" / "pool-advertisement.json").exists())


class BindingsInTheSnapshot(unittest.TestCase):
    def test_each_bound_room_is_a_pinned_row_for_its_worker(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "alpha", "state": "live"}},
                          {"!a:x": "w1"}, version=1)
        snap = pa.snapshot(pr.load_roster(ws), now=1)
        self.assertEqual(snap["bindings"],
                         {"!a:x": {"instance": "w1", "instances": ["w1"], "pinned": True}})
        # A binding with no worker (a cleared pin) is not a row.
        self.assertEqual(pa.bindings({"bindings": {"!b:x": ""}}), {})

    def test_no_bindings_is_an_empty_map_not_a_missing_key(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "alpha", "state": "live"}}, {}, version=1)
        self.assertEqual(pa.snapshot(pr.load_roster(ws), now=1)["bindings"], {})


class EnsureAtBoot(unittest.TestCase):
    def _ws(self, version=1):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "alpha", "state": "live"}}, {}, version=version)
        return ws

    def test_an_existing_roster_without_the_file_gets_one(self):
        ws = self._ws()
        path = pa.ensure_advertisement(ws, now=5)
        self.assertEqual(path, pa.advertisement_path(ws))
        self.assertEqual(json.loads(path.read_text())["workers"]["roster_version"], 1)

    def test_calling_ensure_twice_with_nothing_changed_writes_once(self):
        ws = self._ws()
        path = pa.ensure_advertisement(ws, now=5)
        first = path.stat().st_mtime_ns
        self.assertEqual(pa.ensure_advertisement(ws, now=9), path)
        self.assertEqual(path.stat().st_mtime_ns, first)

    def test_a_current_file_is_left_alone(self):
        ws = self._ws()
        path = pa.write_advertisement(ws, now=5)
        before = path.read_bytes(), path.stat().st_mtime_ns
        pa.ensure_advertisement(ws, now=9)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_a_roster_that_moved_past_the_file_rewrites_it(self):
        ws = self._ws()
        pa.write_advertisement(ws, now=5)
        pr.compile_roster(ws, {"w1": {"label": "alpha", "state": "live"},
                              "w2": {"label": "beta", "state": "live"}}, {}, version=2)
        got = json.loads(pa.ensure_advertisement(ws, now=9).read_text())
        self.assertEqual(got["workers"]["roster_version"], 2)
        self.assertEqual(sorted(got["profile_workers"]), ["w1", "w2"])

    def test_no_roster_means_nothing_and_is_not_an_error(self):
        ws = Path(tempfile.mkdtemp())
        self.assertIsNone(pa.ensure_advertisement(ws))
        self.assertFalse(pa.advertisement_path(ws).exists())
        self.assertEqual(pa.main(["--workspace", str(ws), "--ensure"]), 0)

    def test_the_cli_ensure_writes_for_an_existing_roster(self):
        ws = self._ws()
        self.assertEqual(pa.main(["--workspace", str(ws), "--ensure"]), 0)
        self.assertTrue(pa.advertisement_path(ws).exists())



class TheReportedLeg(unittest.TestCase):
    ROSTER = {"version": 7, "config_version": 3,
              "workers": {"w1": {"label": "Mars", "state": "live", "runtime": "claude"},
                          "w2": {"label": "Beta", "state": "recovering"},
                          "w3": {"label": "Old", "state": "retired"}},
              "bindings": {"!a:x": "w1"}}

    def test_facts_verbatim_and_applied_config_apart(self):
        rep = pa.report(self.ROSTER, now=5)
        self.assertEqual(rep["ts"], 5)
        self.assertEqual(rep["roster_version"], 7)
        self.assertEqual(rep["workers"], [{"id": "w1", "state": "live", "runtime": "claude"},
                                          {"id": "w2", "state": "recovering"},
                                          {"id": "w3", "state": "retired"}])
        self.assertEqual(rep["applied"], {"config_version": 3,
                                          "labels": {"w1": "Mars", "w2": "Beta"},
                                          "bindings": {"!a:x": {"instance": "w1", "instances": ["w1"], "pinned": True}}})

    def test_a_roster_without_a_config_version_reports_none(self):
        self.assertIsNone(pa.report({"version": 1, "workers": {}, "bindings": {}}, now=1)["applied"]["config_version"])

    def test_recovering_is_not_answering_in_the_compat_view(self):
        snap = pa.snapshot(self.ROSTER, now=5)
        self.assertEqual(snap["live_cores"], ["w1"])
        self.assertEqual(snap["dead_cores"], ["w2"])  # never "available"

    def test_the_compat_bodies_are_projections_of_the_report(self):
        rep = pa.report(self.ROSTER, now=5)
        snap = pa.snapshot(self.ROSTER, now=5)
        self.assertEqual(snap["bindings"], rep["applied"]["bindings"])
        self.assertEqual(snap["roster_version"], rep["roster_version"])
        self.assertEqual(pa.profile_workers(self.ROSTER),
                         {"w1": {"label": "Mars", "runtime": "claude"}, "w2": {"label": "Beta"}})

    def test_the_file_carries_the_report_beside_the_compat_bodies(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "alpha", "state": "recovering"}}, {}, version=2)
        got = json.loads(pa.write_advertisement(ws, now=9).read_text())
        self.assertEqual(sorted(got), ["profile_workers", "report", "ts", "workers"])
        self.assertEqual(got["report"]["workers"], [{"id": "w1", "state": "recovering"}])
        self.assertEqual(got["workers"]["dead_cores"], ["w1"])


class ReviewedShapes(unittest.TestCase):
    """The roster's accepted binding forms, schema upgrades, and two writers."""

    def test_a_one_member_list_binding_is_advertised_like_a_scalar(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "live"}}, {"!a:x": ["w1"]}, version=1)
        got = pa.snapshot(pr.load_roster(ws), now=1)["bindings"]
        self.assertEqual(got, {"!a:x": {"instance": "w1", "instances": ["w1"], "pinned": True}})

    def test_a_binding_to_a_retired_worker_is_not_advertised(self):
        r = {"version": 2, "workers": {"w1": {"label": "a", "state": "retired"}}, "bindings": {"!a:x": "w1"}}
        self.assertEqual(pa.bindings(r), {})
        self.assertNotIn("w1", pa.profile_workers(r))

    def test_a_same_version_file_of_an_older_schema_is_rebuilt_at_boot(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "recovering"}}, {}, version=3)
        path = pa.advertisement_path(ws); path.parent.mkdir(parents=True, exist_ok=True)
        old = {"ts": 1, "workers": {"ts": 1, "live_cores": [], "dead_cores": [], "bindings": {},
                                    "roster_version": 3}, "profile_workers": {"w1": {"label": "a"}}}
        path.write_text(json.dumps(old))
        pa.ensure_advertisement(ws, now=5)
        got = json.loads(path.read_text())
        self.assertIn("report", got)
        self.assertEqual(got["workers"]["dead_cores"], ["w1"])  # recovering: not answering

    def test_a_current_file_is_left_alone_by_content(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "live"}}, {}, version=3)
        path = pa.write_advertisement(ws, now=5)
        before = path.stat().st_mtime_ns
        pa.ensure_advertisement(ws, now=9)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_a_writer_that_lost_the_race_does_not_regress_a_newer_publication(self):
        import threading
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "live"}}, {}, version=10)
        real_replace = os.replace
        paused = threading.Event(); release = threading.Event()

        def slow_replace(src, dst):
            if not paused.is_set():
                paused.set(); release.wait(5)
            return real_replace(src, dst)

        errors = []
        def old_writer():
            try:
                with unittest.mock.patch.object(pa.os, "replace", slow_replace):
                    pa.write_advertisement(ws, now=1)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        t = threading.Thread(target=old_writer); t.start()
        self.assertTrue(paused.wait(5))
        # v11 retires the worker; its writer must wait for the lock, then publish
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "retired"}}, {}, version=11)
        newer = threading.Thread(target=lambda: pa.write_advertisement(ws, now=2)); newer.start()
        release.set(); t.join(5); newer.join(5)
        got = json.loads(pa.advertisement_path(ws).read_text())
        self.assertEqual(errors, [])
        self.assertEqual(got["report"]["roster_version"], 11)
        self.assertEqual(got["workers"]["live_cores"], [])
        # and a late v10 write after v11 is refused outright
        # The derived file follows the roster even to a lower version (kewei, #4210).
        pr.compile_roster(ws, {"w1": {"label": "a", "state": "live"}}, {}, version=10)
        pa.write_advertisement(ws, now=3)
        self.assertEqual(json.loads(pa.advertisement_path(ws).read_text())["report"]["roster_version"], 10)


class TestEnsureComparesEveryHalf(unittest.TestCase):
    """A file whose report matches but whose other halves are stale is rewritten
    (kewei, #4119): the comparison covers the whole published projection."""

    def test_a_stale_profile_half_forces_a_rewrite(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"state": "live", "label": "one"}}, {})
        path = pa.write_advertisement(ws, now=10)
        doc = json.loads(path.read_text())
        doc["profile_workers"] = {"w1": {"label": "STALE"}}
        path.write_text(json.dumps(doc))
        pa.ensure_advertisement(ws, now=11)
        self.assertEqual(json.loads(path.read_text())["profile_workers"]["w1"]["label"], "one")


class TestTheHandlerPublishesAnInheritedRoster(unittest.TestCase):
    """A roster that predates the advertiser (or was compiled while nothing ran)
    is published the first time the route handler runs, before any routing."""

    def test_a_task_through_the_handler_writes_the_advertisement(self):
        import pool_route_handler as prh
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"state": "live", "label": "one"}}, {}, version=7)
        self.assertFalse(pa.advertisement_path(ws).exists())
        task = ws / "task-x.txt"
        task.write_text("id: task-x\nsource: ag2space\nchannel_id: !r:ag2.space\naccess_tier: owner\ntask: hello\n")
        with contextlib.redirect_stderr(io.StringIO()):
            prh.main(["--task-file", str(task), "--workspace", str(ws), "--probe"])
        got = json.loads(pa.advertisement_path(ws).read_text())
        self.assertEqual(got["report"]["roster_version"], 7)
        self.assertEqual(got["workers"]["live_cores"], ["w1"])

    def test_a_stale_file_at_a_higher_version_is_repaired(self):
        ws = Path(tempfile.mkdtemp())
        pr.compile_roster(ws, {"w1": {"state": "live", "label": "one"}}, {}, version=1)
        path = pa.write_advertisement(ws, now=1)
        doc = json.loads(path.read_text()); doc["workers"]["roster_version"] = 999; doc["workers"]["live_cores"] = []
        path.write_text(json.dumps(doc))
        pa.ensure_advertisement(ws, now=2)
        self.assertEqual(json.loads(path.read_text())["workers"]["live_cores"], ["w1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
