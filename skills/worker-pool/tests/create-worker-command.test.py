#!/usr/bin/env python3
"""One command, or the roster goes stale: create-worker composes the four steps.

The defect it closes: spawning wrote an identity while the roster was recompiled
separately, so a missed step left a worker that runs and cannot be addressed.

Run: python3 skills/worker-pool/tests/create-worker-command.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import create_worker as cw  # noqa: E402

import pool_roster as pr  # noqa: E402

import spawn_worker as sw  # noqa: E402

ROOM = "!abc:ag2.space"
ROOM2 = "!def:ag2.space"


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
        self._real_core_runtime = sw.core_runtime
        sw.core_runtime = lambda repo, runner=None: "claude"
        self.addCleanup(lambda: setattr(sw, "core_runtime", self._real_core_runtime))

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

    def test_a_second_create_keeps_the_first_rooms_binding(self):
        # The next call reloads bindings.json, not the roster, so a binding
        # that lived only in the compiled roster vanished on the second create.
        self.assertEqual(self.run_cli("--room", ROOM), 0)
        self.assertEqual(self.run_cli("--room", ROOM2), 0)
        roster = pr.load_roster(self.ws)
        want = {ROOM: self.spawned[0], ROOM2: self.spawned[1]}
        self.assertEqual(roster["bindings"], want)
        self.assertEqual(pr.load_bindings(self.ws), want)
        self.assertEqual(pr.targets_for(roster, ROOM), [self.spawned[0]])
        self.assertEqual(pr.targets_for(roster, ROOM2), [self.spawned[1]])


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


class TestARefusalNamesTheRetainedWorker(Base):
    """A launcher failure must not claim a clean refusal while a record and a
    delivery dir it just minted stay on disk, unrostered and unnamed."""

    def test_a_launcher_failure_leaves_no_record_or_delivery_dir(self):
        def fake_spawn(workspace, repo, **kw):
            raise sw.SpawnRefused("the runtime launcher failed: did not come up")
        sw.spawn = fake_spawn
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.run_cli()
        self.assertEqual(rc, cw.REFUSED)
        self.assertIn("the runtime launcher failed", err.getvalue())
        self.assertEqual(self.spawned, [])
        self.assertFalse((self.ws / "state" / "workers").exists())
        self.assertFalse((self.ws / "deliveries").exists())


class TestDryRunPredictsTheSameRuntimeAsTheRealRun(Base):
    """Two different runtimes for one command is a prediction the real run is
    free to break; dry-run and the real spawn must resolve it identically."""

    def test_omitted_runtime_with_a_supported_core_matches(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("--dry-run"), 0)
        planned = json.loads(out.getvalue())["runtime"]
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(planned, "claude")

    def test_omitted_runtime_with_an_unsupported_core_refuses_both_ways(self):
        sw.core_runtime = lambda repo, runner=None: "codex"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            dry_rc = self.run_cli("--dry-run")
        self.assertEqual(dry_rc, cw.REFUSED)
        self.assertIn("worker mode", err.getvalue())
        self.assertEqual(self.spawned, [])

        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            real_rc = self.run_cli()
        self.assertEqual(real_rc, cw.REFUSED)
        self.assertIn("worker mode", err2.getvalue())
        self.assertEqual(self.spawned, [])

    def test_explicit_supported_runtime_is_honored(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("--dry-run", "--runtime", "claude"), 0)
        self.assertEqual(json.loads(out.getvalue())["runtime"], "claude")
        self.assertEqual(self.run_cli("--runtime", "claude"), 0)

    def test_explicit_bogus_runtime_refuses_both_ways(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            dry_rc = self.run_cli("--dry-run", "--runtime", "bogus")
        self.assertEqual(dry_rc, cw.REFUSED)
        self.assertIn("worker mode", err.getvalue())

        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            real_rc = self.run_cli("--runtime", "bogus")
        self.assertEqual(real_rc, cw.REFUSED)
        self.assertIn("worker mode", err2.getvalue())
        self.assertEqual(self.spawned, [])


class TestTheWriterRefusesAnUnreadableOrMalformedRosterViaTheCli(Base):
    """The same absent/unreadable/malformed distinction, exercised end-to-end
    through the command a caller actually runs, not just the writer directly."""

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_an_existing_unreadable_roster_refuses_and_is_untouched(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 7, "workers": {"a" * 32: {
            "label": "keeper", "state": "idle"}}, "bindings": {}}))
        before = p.read_bytes()
        p.chmod(0o000)
        self.addCleanup(lambda: p.chmod(0o644))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.run_cli()
        self.assertNotEqual(rc, 0)
        self.assertIn(self.spawned[0], err.getvalue())
        p.chmod(0o644)
        self.assertEqual(p.read_bytes(), before)

    def test_an_existing_malformed_roster_refuses_and_is_untouched(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        rc = self.run_cli()
        self.assertNotEqual(rc, 0)
        self.assertEqual(p.read_text(), "{not json")

    def test_an_absent_roster_still_succeeds(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(set(pr.load_roster(self.ws)["workers"]), {self.spawned[0]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
