#!/usr/bin/env python3
"""A worker's declarations keep exact credentials while folder migration replaces every lane."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("multi_runtime_base", ROOT / "tests/worker-lane-sparrow-bridge.test.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
C, L, W = S.C, S.worker_lane, S.W


class MultiIdentity(S.Base):
    def setUp(self):
        super().setUp()
        self.add(S.H.TOKEN_B, fetch=S.H.agents("@sutando:beta-fixture.net"))
        self.token = "https://chat.beta-fixture.net|second-worker-secret"
        preview = C.preview_connection(self.token, fetch=S.H.agents("@reviewer:beta-fixture.net"))
        self.second, _ = C.add_worker_identity(W, self.token, expected_server=preview["server"],
                                               preview=preview, workspace=self.ws, vault=self.vault)

    def test_each_declaration_uses_its_own_credential_and_revision(self):
        revisions = []
        for row, token in ((self.row, S.TOKEN_W), (self.second, self.token)):
            instance = row["instance"]
            rc, said = self.cli("declare", "--worker", W, "--instance", instance)
            self.assertEqual(rc, 0, said)
            self.assertTrue(L.declared_bridge(W, self.ws, instance=instance))
            self.assertEqual(L.credential(W, self.ws, {}, self.vault, instance=instance), (token, ""))
            declaration, why = L._declaration_for(W, self.ws, instance=instance)
            loaded, why = S.bridge.load(declaration["path"], self.agents)
            self.assertEqual(why, "")
            self.assertEqual(loaded["instance"], instance)
            revisions.append(loaded["digest"])
        self.assertNotEqual(*revisions)
        self.assertEqual(L.credential("a" * 32, self.ws, {}, self.vault,
                                      instance=self.second["instance"])[0], "")

    def test_a_declaration_does_not_claim_the_other_lane(self):
        rc, said = self.cli("declare", "--worker", W, "--instance", self.row["instance"])
        self.assertEqual(rc, 0, said)
        self.assertFalse(L.declared_bridge(W, self.ws, instance=self.second["instance"]))

    def test_migration_stops_both_lanes_and_uses_one_folder(self):
        stopped = []
        with mock.patch.object(C, "bridge_pid", return_value=4242), \
                mock.patch.object(C, "_stop_row", side_effect=lambda row, *a: stopped.append(row["instance"])):
            out, why = L.migrate_dirs(W, self.ws, handler="", procs={},
                                      start=lambda instance, ws: {"ok": True, "lease": "held", "pid": 1})
        self.assertEqual(why, "")
        self.assertCountEqual(stopped, [self.row["instance"], self.second["instance"]])
        for row in (self.row, self.second):
            tasks, results, own = L.lane_dirs(self.ws, row["instance"])
            self.assertTrue(own)
            self.assertEqual((str(tasks), str(results)), (out["tasks"], out["results"]))
        preview, why = L.preview_migrate(W, self.ws, undo=True, procs={})
        self.assertEqual(why, "")
        self.assertEqual(set(preview["lanes"]), set(stopped))

    def test_every_runtime_revision_tracks_the_worker_folder_switch(self):
        instances = [self.row["instance"], self.second["instance"]]
        shared = [L.runtime_identity(W, instance, self.ws) for instance in instances]
        C.set_worker_dirs(W, True, workspace=self.ws)
        own = [L.runtime_identity(W, instance, self.ws) for instance in instances]
        self.assertEqual(own, [revision + b"\0own" for revision in shared])
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertEqual([L.runtime_identity(W, instance, self.ws) for instance in instances], shared)

    def test_a_lingering_second_lane_prevents_moving_files(self):
        with mock.patch.object(L, "_stop_unless_declared", side_effect=[
                ("not-running", 0, ""), ("declared", 4242, "")]), \
                mock.patch.object(L, "_await_replacement", return_value=("", "pending")):
            out, why = L.migrate_dirs(W, self.ws, handler="", procs={})
        self.assertEqual(why, "pending")
        self.assertNotIn("results_moved", out)

    def test_migration_waits_for_both_declared_holders(self):
        waited = []
        with mock.patch.object(L, "_stop_unless_declared", return_value=("declared", 4242, "")), \
                mock.patch.object(L, "_await_replacement", side_effect=lambda *a, **kw:
                                  (waited.append(kw["instance"]) or "restarted-by-sparrowd", "")):
            kind, why = L.relaunch_lane(W, self.ws)
        self.assertEqual(why, "")
        self.assertCountEqual(waited, [self.row["instance"], self.second["instance"]])


if __name__ == "__main__":
    unittest.main()
