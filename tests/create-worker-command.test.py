#!/usr/bin/env python3
"""One command, or the roster goes stale: create-worker composes the four steps.

The defect it closes: spawning wrote an identity while the roster was recompiled
separately, so a missed step left a worker that runs and cannot be addressed.

Run: python3 tests/create-worker-command.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pool_roster as pr  # noqa: E402
import spawn_worker as sw  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "create_worker", REPO / "scripts" / "create-worker.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

ROOM = "!abc:ag2.space"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop("SUTANDO_INSTANCE_ID", None)
        self.spawned = []

        def fake_spawn(workspace, repo, **kw):
            wid = f"{len(self.spawned):032x}"
            self.spawned.append(wid)
            (Path(workspace) / "deliveries" / wid).mkdir(parents=True)
            (Path(workspace) / "state" / "workers" / wid).mkdir(parents=True)
            return {"worker_id": wid, "label": kw.get("label") or wid,
                    "cwd": kw.get("cwd") or str(repo),
                    "delivery_dir": str(Path(workspace) / "deliveries" / wid),
                    "tmux": {"socket": "/tmp/s.sock", "session_name": f"w-{wid}"}}

        self._real_spawn, sw.spawn = sw.spawn, fake_spawn
        self.addCleanup(lambda: setattr(sw, "spawn", self._real_spawn))
        self._real_gate = sw.per_instance_sentinel_supported
        sw.per_instance_sentinel_supported = lambda repo: True
        cw.sw = sw
        self.addCleanup(lambda: setattr(sw, "per_instance_sentinel_supported",
                                        self._real_gate))

    def run_cli(self, *args):
        return cw.main(["--workspace", str(self.ws), "--repo", str(REPO), *args])


class TestTheRosterCannotGoStale(Base):
    def test_creating_a_worker_puts_it_in_the_roster(self):
        self.assertEqual(self.run_cli("--label", "reviewer"), 0)
        roster = pr.load_roster(self.ws)
        self.assertIn(self.spawned[0], roster["workers"])
        self.assertEqual(roster["workers"][self.spawned[0]]["label"], "reviewer")

    def test_a_second_worker_does_not_evict_the_first(self):
        self.run_cli("--label", "one")
        self.run_cli("--label", "two")
        self.assertEqual(sorted(pr.load_roster(self.ws)["workers"]),
                         sorted(self.spawned))

    def test_a_room_is_bound_in_the_same_command(self):
        self.run_cli("--room", ROOM)
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["bindings"][ROOM], self.spawned[0])
        self.assertEqual(pr.targets_for(roster, ROOM), [self.spawned[0]])

    def test_without_a_room_nothing_is_bound(self):
        self.run_cli()
        self.assertEqual(pr.load_roster(self.ws)["bindings"], {})


class TestItRefusesBeforeCreating(Base):
    def test_a_worker_may_not_create_workers(self):
        os.environ["SUTANDO_INSTANCE_ID"] = "deadbeef"
        self.assertEqual(self.run_cli(), cw.REFUSED)
        self.assertEqual(self.spawned, [])

    def test_an_empty_room_refuses_before_spawning(self):
        self.assertEqual(self.run_cli("--room", "  "), cw.REFUSED)
        self.assertEqual(self.spawned, [])

    def test_a_checkout_without_the_per_instance_sentinel_refuses(self):
        sw.per_instance_sentinel_supported = lambda repo: False
        self.assertEqual(self.run_cli(), cw.REFUSED)
        self.assertEqual(self.spawned, [])

    def test_dry_run_creates_nothing(self):
        self.assertEqual(self.run_cli("--dry-run"), 0)
        self.assertEqual(self.spawned, [])
        self.assertIsNone(pr.load_roster(self.ws))


class TestUnrosteredRecordsAreReportedNotAdopted(Base):
    def test_a_record_with_no_roster_entry_is_listed(self):
        orphan = "f" * 32
        (self.ws / "state" / "workers" / orphan).mkdir(parents=True)
        self.run_cli()
        roster = pr.load_roster(self.ws)
        self.assertNotIn(orphan, roster["workers"])
        self.assertIn(orphan, cw.unrostered(self.ws, roster["workers"]))

    def test_an_existing_roster_entry_is_preserved_not_rebuilt(self):
        pr.compile_roster(self.ws, {"a" * 32: {"state": "recovering",
                                               "label": "old"}}, {})
        self.run_cli()
        kept = pr.load_roster(self.ws)["workers"]["a" * 32]
        self.assertEqual(kept["state"], "recovering")


if __name__ == "__main__":
    unittest.main(verbosity=2)
