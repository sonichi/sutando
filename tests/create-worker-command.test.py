#!/usr/bin/env python3
"""One command, or the roster goes stale: create-worker composes the four steps.

The defect it closes: spawning wrote an identity while the roster was recompiled
separately, so a missed step left a worker that runs and cannot be addressed.

Run: python3 tests/create-worker-command.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
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
                    "runtime": kw.get("runtime") or "claude",
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
    def test_creating_a_worker_writes_the_advertisement_file(self):
        self.run_cli("--label", "alpha")
        got = json.loads((self.ws / "state" / "pool-advertisement.json").read_text())
        self.assertEqual(sorted(got), ["profile_workers", "ts", "workers"])
        labels = [w["label"] for w in got["profile_workers"].values()]
        self.assertEqual(labels, ["alpha"])
        self.assertEqual(len(got["workers"]["live_cores"]) + len(got["workers"]["dead_cores"]), 1)

    def test_the_resolved_runtime_reaches_the_roster_and_the_advertisement(self):
        self.assertEqual(self.run_cli("--label", "Research", "--runtime", "codex"), 0)
        wid = self.spawned[0]
        self.assertEqual(pr.load_roster(self.ws)["workers"][wid]["runtime"], "codex")
        got = json.loads((self.ws / "state" / "pool-advertisement.json").read_text())
        self.assertEqual(got["profile_workers"][wid], {"label": "Research", "runtime": "codex"})

    def test_an_advertisement_write_failure_is_reported_as_itself(self):
        real = cw.pa.write_advertisement
        cw.pa.write_advertisement = lambda ws, now=None: (_ for _ in ()).throw(OSError(28, "No space left"))
        self.addCleanup(lambda: setattr(cw.pa, "write_advertisement", real))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.run_cli("--label", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn(self.spawned[0], pr.load_roster(self.ws)["workers"])  # routable
        self.assertIn("is routable", err.getvalue())
        self.assertIn("advertisement could not be written", err.getvalue())
        self.assertNotIn("roster could not be compiled", err.getvalue())
        self.assertFalse((self.ws / "state" / "pool-advertisement.json").exists())

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


class TestFailuresAreLoud(Base):
    """The two paths that leave the owner without a usable worker: both must
    say so on stderr and exit non-zero, never report success quietly."""

    def test_a_missing_repo_refuses(self):
        self.assertEqual(
            cw.main(["--workspace", str(self.ws), "--repo",
                     str(self.ws / "nope")]), cw.REFUSED)
        self.assertEqual(self.spawned, [])

    def test_a_refused_spawn_exits_refused(self):
        def boom(*a, **kw):
            raise sw.SpawnRefused("tmux session already exists")
        sw.spawn = boom
        self.assertEqual(self.run_cli(), cw.REFUSED)

    def test_a_worker_created_but_unrostered_fails_loudly(self):
        # The worst outcome: the worker exists and routing cannot see it. It
        # must not exit 0, or the caller believes the worker is usable.
        def bad_roster(*a, **kw):
            raise pr.RosterError("binding names a worker that does not exist")
        pr.compile_roster, real = bad_roster, pr.compile_roster
        self.addCleanup(lambda: setattr(pr, "compile_roster", real))
        cw.pr = pr
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("was created, but the", err.getvalue())
        self.assertIn(self.spawned[0], err.getvalue())


class TestJsonOutput(Base):
    def test_json_carries_the_ids_a_script_needs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("--json", "--room", ROOM), 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got["worker_id"], self.spawned[0])
        self.assertEqual(got["room"], ROOM)
        self.assertEqual(got["roster_version"], pr.load_roster(self.ws)["version"])


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
