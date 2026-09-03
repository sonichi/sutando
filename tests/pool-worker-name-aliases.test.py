#!/usr/bin/env python3
"""One-release read aliases for the pre-rename `core-N` worker spelling.

Nothing writes `core-N` any more, but state left by a pre-rename pool must
still be understood: legacy beats, legacy assigned/claimed suffixes, legacy
rows in the affinity / no-claim / notify tables, a legacy channel handler,
and the deprecated script names (3-line shims that exec the new scripts).

Run: python3 tests/pool-worker-name-aliases.test.py
"""
# flake8: noqa: E402 — imports follow the sys.path bootstrap
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
sys.path.insert(0, str(REPO / "scripts"))
import claim_task
import pool_follower
from pool_lead import PoolLead
from pool_notify import PoolNotifier


def _ws(td: Path) -> Path:
    for d in ("tasks", "results", "state/cores", "state/pool"):
        (td / d).mkdir(parents=True, exist_ok=True)
    return td


class LegacyBeatDiscoveryTest(unittest.TestCase):
    """The daemon's follower enumeration reads `core-N.alive` as `worker-N`."""

    def test_daemon_reads_legacy_beats_as_workers(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pool_lead_daemon", REPO / "scripts" / "pool-lead-daemon.py")
        daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon)
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            cores = ws / "state" / "cores"
            (cores / "core-2.alive").write_text("beat")
            (cores / "worker-3.alive").write_text("beat")
            (cores / "core-3.alive").write_text("beat")  # same seat, twice
            (cores / "pool-lead.alive").write_text("beat")
            seen = {}
            real_ws, real_argv = daemon._workspace, sys.argv
            real_lead, real_status = daemon.PoolLead, daemon.PoolStatusWriter

            class Probe:
                def __init__(self, tasks, state, followers, alive, **kw):
                    seen["followers"] = followers()
                    seen["alive"] = {i: alive(i) for i in seen["followers"]}
                    seen["alive_missing"] = alive("worker-9")
                    raise SystemExit(0)

            daemon._workspace = lambda: ws
            daemon.PoolLead = Probe
            sys.argv = ["pool-lead-daemon.py"]
            try:
                with self.assertRaises(SystemExit):
                    daemon.main()
            finally:
                daemon._workspace, sys.argv = real_ws, real_argv
                daemon.PoolLead, daemon.PoolStatusWriter = real_lead, real_status
            self.assertEqual(sorted(seen["followers"]), ["worker-2", "worker-3"])
            self.assertEqual(seen["alive"], {"worker-2": True, "worker-3": True})
            self.assertFalse(seen["alive_missing"])

    def test_lead_dedupes_a_seat_seen_under_both_spellings(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            lead = PoolLead(ws / "tasks", ws / "state",
                            followers_fn=lambda: ["core-1", "worker-1", "core-2"],
                            alive_fn=lambda i: i in {"worker-1", "worker-2"})
            self.assertEqual(lead._live_followers(), ["worker-1", "worker-2"])


class LegacySuffixTest(unittest.TestCase):
    def test_follower_claims_a_legacy_assignment_with_the_new_suffix(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            (ws / "tasks" / "task-a.assigned-core-2.txt").write_text("task: a\n")
            (ws / "state" / "cores" / "pool-lead.alive").write_text("beat")
            got = pool_follower.acquire_work(ws / "tasks", ws / "state",
                                             "worker-2", "pool-lead")
            self.assertEqual(got.name, "task-a.claimed-worker-2.txt")
            # a legacy instance argument resolves to the same worker
            (ws / "tasks" / "task-b.assigned-worker-2.txt").write_text("task: b\n")
            got = pool_follower.acquire_work(ws / "tasks", ws / "state",
                                             "core-2", "pool-lead")
            self.assertEqual(got.name, "task-b.claimed-worker-2.txt")

    def test_finish_accepts_a_legacy_claim_and_flags_under_the_new_name(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            claimed = ws / "tasks" / "task-c.claimed-core-1.txt"
            claimed.write_text("id: task-c\nsource: chat\ntask: x\n")
            pool_follower.finish_task(ws / "tasks", ws / "results", ws / "state",
                                      "worker-1", claimed, "task: c\nbody\n")
            self.assertTrue((ws / "results" / "task-c.txt").exists())
            self.assertTrue((ws / "state" / "cores" / "worker-1" / "done"
                             / "task-c.flag").exists())
            self.assertFalse((ws / "state" / "cores" / "core-1").exists())

    def test_lead_load_and_reclaim_see_legacy_suffixes(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            tasks = ws / "tasks"
            (tasks / "task-x.claimed-core-1.txt").write_text("x")
            (tasks / "task-y.assigned-worker-1.txt").write_text("y")
            (tasks / "task-z.assigned-core-2.txt").write_text("z")
            lead = PoolLead(tasks, ws / "state",
                            followers_fn=lambda: ["worker-1"],
                            alive_fn=lambda i: i == "worker-1")
            self.assertEqual(lead._load("worker-1"), 2)
            self.assertEqual(lead._claimed_load("worker-1"), 1)
            # worker-2 is dead under either spelling: its legacy assignment repools
            self.assertEqual(lead.reclaim_dead(), ["task-z.assigned-core-2.txt"])
            self.assertTrue((tasks / "task-z.txt").exists())

    def test_notify_done_flag_under_either_dir(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            flag = ws / "state" / "cores" / "core-3" / "done" / "task-q.flag"
            flag.parent.mkdir(parents=True)
            flag.write_text("")
            n = PoolNotifier(ws / "tasks", ws / "state", lambda *a: True)
            self.assertTrue(n._done_flag("task-q", "worker-3"))


class LegacyTableRowsTest(unittest.TestCase):
    def test_affinity_rows_canonicalise_on_load_and_write_back(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            (ws / "state" / "pool" / "affinity.json").write_text(json.dumps({
                "!room:a": {"instance": "core-1", "ts": 1.0, "pinned": True},
                "!room:b": {"instance": "core-2", "instances": ["core-2", "worker-3"],
                            "ts": 1.0, "pinned": True},
                "chan-c": {"instance": "worker-1", "ts": 1.0}}))
            lead = PoolLead(ws / "tasks", ws / "state",
                            followers_fn=lambda: ["worker-1"],
                            alive_fn=lambda i: i == "worker-1")
            b = lead.bindings()
            self.assertEqual(b["!room:a"]["instance"], "worker-1")
            self.assertEqual(b["!room:b"]["instances"], ["worker-2", "worker-3"])
            self.assertEqual(b["!room:b"]["instance"], "worker-2")
            # a legacy pin request is stored canonical
            lead.pin_room("!room:d", "core-4")
            on_disk = json.loads((ws / "state" / "pool" / "affinity.json").read_text())
            self.assertEqual(on_disk["!room:d"]["instance"], "worker-4")
            # a sweep that touches the table rewrites every row canonical
            (ws / "tasks" / "task-1.txt").write_text(
                "id: task-1\nsource: chat\nchannel_id: chan-c\ntask: t\n")
            lead.sweep()
            on_disk = json.loads((ws / "state" / "pool" / "affinity.json").read_text())
            self.assertEqual(on_disk["!room:a"]["instance"], "worker-1")
            self.assertNotIn("core-", json.dumps(on_disk))

    def test_noclaim_and_notify_ledgers_canonicalise_on_load(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            (ws / "state" / "pool" / "no-claim.json").write_text(
                json.dumps({"core-2": time.time()}))
            lead = PoolLead(ws / "tasks", ws / "state",
                            followers_fn=lambda: [], alive_fn=lambda _i: False)
            self.assertFalse(lead._claiming("worker-2"))
            (ws / "state" / "pool" / "notify-ledger.json").write_text(
                json.dumps({"channels": {"chan": "core-1"}, "tasks": {}}))
            n = PoolNotifier(ws / "tasks", ws / "state", lambda *a: True)
            self.assertEqual(n._load()["channels"], {"chan": "worker-1"})


class LegacyClaimTaskTest(unittest.TestCase):
    def test_legacy_handler_and_beat_are_the_same_worker(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            (ws / "tasks" / "task-h.txt").write_text("x")
            (ws / "state" / "cores" / "core-1.alive").write_text("beat")
            hp = claim_task._handler_path(ws, "chan")
            hp.write_text(json.dumps({"core_id": "1", "last_handled_at": time.time()}))
            # handler is worker-1, alive via its legacy beat: worker-2 must yield
            self.assertIsNone(claim_task.claim_with_affinity("h", "2", "chan", workspace=ws))
            got = claim_task.claim_with_affinity("h", "core-1", "chan", workspace=ws)
            self.assertEqual(got.name, "task-h.claimed-worker-1.txt")
            self.assertEqual(json.loads(hp.read_text())["worker"], "worker-1")

    def test_seat_and_either_spelling_claim_the_same_file(self):
        with tempfile.TemporaryDirectory() as t:
            ws = _ws(Path(t))
            for arg in ("3", "worker-3", "core-3"):
                (ws / "tasks" / f"task-{arg}.txt").write_text("x")
                got = claim_task.claim_plain(arg, arg, workspace=ws)
                self.assertEqual(got.name, f"task-{arg}.claimed-worker-3.txt")


class ShimTest(unittest.TestCase):
    """The deprecated script names exec the new ones with the same args."""

    def _run_shim(self, td: Path, old: str, new: str):
        scripts = td / "scripts"
        scripts.mkdir()
        shutil.copy(REPO / "scripts" / old, scripts / old)
        (scripts / new).write_text('#!/bin/bash\nprintf "NEW:%s\\n" "$@"\n')
        os.chmod(scripts / new, 0o755)
        return subprocess.run(["bash", str(scripts / old), "3", "--check-only"],
                              capture_output=True, text=True, timeout=30)

    def test_each_shim_execs_the_new_script(self):
        for old, new in (("install-core-pool.sh", "install-worker-pool.sh"),
                         ("uninstall-core-pool.sh", "uninstall-worker-pool.sh"),
                         ("pool-core-wrapper.sh", "pool-worker-wrapper.sh")):
            with tempfile.TemporaryDirectory() as t:
                r = self._run_shim(Path(t), old, new)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "NEW:3\nNEW:--check-only\n", old)
                self.assertIn("deprecated", r.stderr, old)
                self.assertIn(new, r.stderr, old)
            body = (REPO / "scripts" / old).read_text().splitlines()
            self.assertLessEqual(len(body), 4, f"{old} must stay a shim")
            self.assertTrue(body[-1].startswith("exec "), old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
