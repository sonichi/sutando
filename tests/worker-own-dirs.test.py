#!/usr/bin/env python3
"""A converted pool worker's own task and result folders: one source of truth in `connections`.

- `worker_dirs(worker)` is `<workspace>/agents/<segment>/{tasks,results}` only when the worker's identity
  row names an agent and carries `dirs: "own"`; any other worker (no row, no agent, no field), and an
  unreadable record, answers None: the shared tasks/ and results/ stay its.
- The folder is the one a claude-local bridge for that agent uses (`connect_bridge.bridge_env`).
- `task_home(task_id)` maps a lane-queued id to its lane, worker and dirs; anything else is shared.
- `set_worker_dirs` sets and clears the field under the record lock; the schema refuses it elsewhere.
- `worker_lane.runtime_identity` carries the field, and is byte-identical for a shared-dir worker.

Run: python3 tests/worker-own-dirs.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "ag2-sparrow")]
import connections as C, worker_lane  # noqa: E401,E402
from ag2_sparrow import agent_identity, connect_bridge  # noqa: E402

W, OTHER = "7c700e11" + "0" * 24, "8d811f22" + "0" * 24
CORE_AGENT, WORKER_AGENT = "@sutando:alpha-fixture.net", "@reviewer:alpha-fixture.net"
WORKER_INST = C.worker_instance_for("alpha-fixture.net", W)


def row(instance, *, primary=False, **extra):
    base = {"server": "alpha-fixture.net", "instance": instance, "gateway_url": "https://chat.alpha-fixture.net",
            "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": primary,
            "added_by": "owner", "added_at": "2026-11-16T00:00:00Z", "verified": True,
            "server_source": "gateway", "owner_mxid": "@owner:alpha-fixture.net"}
    return {**base, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def write(self, *rows):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "connections": list(rows)}))

    def standard(self, **worker_extra):
        self.write(row("alpha-fixture-net", primary=True, agent_mxid=CORE_AGENT),
                   row(WORKER_INST, worker=W, agent_mxid=WORKER_AGENT, **worker_extra))

    def own_root(self):
        return self.ws / "agents" / agent_identity.root_segment(WORKER_AGENT)


class WorkerDirs(Base):
    def test_own_dirs_are_the_agents_folder(self):
        self.standard(dirs="own")
        root = self.own_root()
        self.assertEqual(C.worker_dirs(W, self.ws), (root / "tasks", root / "results"))
        env = connect_bridge.bridge_env(str(root), WORKER_AGENT)
        self.assertEqual((env["AGENT_CONNECT_TASK_DIR"], env["AGENT_CONNECT_RESULT_DIR"]),
                         (str(root / "tasks"), str(root / "results")))

    def test_shared_without_the_field(self):
        self.standard()
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_shared_without_an_agent(self):
        self.write(row("alpha-fixture-net", primary=True, agent_mxid=CORE_AGENT),
                   row(WORKER_INST, worker=W, dirs="own"))
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_shared_for_another_worker_no_worker_or_no_record(self):
        self.standard(dirs="own")
        self.assertIsNone(C.worker_dirs(OTHER, self.ws))
        self.assertIsNone(C.worker_dirs("", self.ws))
        C.record_path(self.ws).write_text("{not json")
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_schema_refuses_the_field_off_a_worker_row_or_with_another_value(self):
        for rows in ((row("alpha-fixture-net", primary=True, dirs="own"),),
                     (row("alpha-fixture-net", primary=True), row(WORKER_INST, worker=W, dirs="shared"))):
            self.write(*rows)
            with self.assertRaises(C.SchemaError):
                C._read(C.record_path(self.ws))


class TaskHome(Base):
    def test_a_task_the_own_dir_lane_queued_is_in_its_dirs(self):
        self.standard(dirs="own")
        root = self.own_root()
        home = C.task_home(f"task-{WORKER_INST}~abc", self.ws)
        self.assertEqual(home, {"lane": WORKER_INST, "worker": W, "tasks": root / "tasks",
                                "results": root / "results", "own": True})
        self.assertTrue(C.task_home(f"task-{WORKER_INST}~abc~~2", self.ws)["own"])

    def test_a_shared_worker_lane_core_lane_or_plain_id_is_shared(self):
        self.standard()
        shared = {"tasks": self.ws / "tasks", "results": self.ws / "results", "own": False}
        self.assertEqual(C.task_home(f"task-{WORKER_INST}~abc", self.ws),
                         {"lane": WORKER_INST, "worker": W, **shared})
        self.assertEqual(C.task_home("task-alpha-fixture-net~abc", self.ws),
                         {"lane": "alpha-fixture-net", "worker": "", **shared})
        self.assertEqual(C.task_home("task-1700000000000", self.ws), {"lane": "", "worker": "", **shared})
        self.assertEqual(C.task_home("task-unknown-lane~abc", self.ws),
                         {"lane": "unknown-lane", "worker": "", **shared})

    def test_an_id_its_lane_did_not_queue_is_its_when_its_tasks_folder_holds_it(self):
        self.standard(dirs="own")
        root = self.own_root()
        probe = "task-0123abcd"
        self.assertFalse(C.task_home(probe, self.ws)["own"])
        (root / "tasks" / "archive").mkdir(parents=True)
        (root / "tasks" / f"{probe}.txt").write_text("t")
        self.assertEqual(C.task_home(probe, self.ws), {"lane": "", "worker": W, "tasks": root / "tasks",
                                                       "results": root / "results", "own": True})
        (root / "tasks" / f"{probe}.txt").rename(root / "tasks" / f"{probe}.claimed-core-1.txt")
        self.assertTrue(C.task_home(probe, self.ws)["own"], "claimed there still lives there")
        (root / "tasks" / f"{probe}.claimed-core-1.txt").rename(root / "tasks" / "archive" / f"{probe}.txt")
        self.assertTrue(C.task_home(probe, self.ws)["own"], "archived there still lives there")
        self.assertFalse(C.task_home("../escape", self.ws)["own"])

    def test_an_unreadable_record_is_shared(self):
        self.standard(dirs="own")
        C.record_path(self.ws).write_text("{not json")
        home = C.task_home(f"task-{WORKER_INST}~abc", self.ws)
        self.assertEqual((home["worker"], home["own"], home["tasks"]), ("", False, self.ws / "tasks"))

    def test_a_launch_key_alias_maps_to_the_row(self):
        self.standard(dirs="own", launch_keys=["AG2_REMOTE_TOKEN_REVIEWER"])
        self.assertTrue(C.task_home("task-reviewer~abc", self.ws)["own"])


class SetWorkerDirs(Base):
    def test_set_and_clear(self):
        self.standard()
        self.assertEqual(C.set_worker_dirs(W, True, workspace=self.ws)["dirs"], "own")
        self.assertIsNotNone(C.worker_dirs(W, self.ws))
        self.assertNotIn("dirs", C.set_worker_dirs(W, False, workspace=self.ws))
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertNotIn("dirs", C.worker_identity(W, self.ws))

    def test_refused_without_a_row_or_an_agent(self):
        self.standard()
        with self.assertRaises(C.ConnectionsError):
            C.set_worker_dirs(OTHER, True, workspace=self.ws)
        self.write(row("alpha-fixture-net", primary=True), row(WORKER_INST, worker=W))
        with self.assertRaises(C.ConnectionsError):
            C.set_worker_dirs(W, True, workspace=self.ws)


class MultipleIdentityStorage(Base):
    def identities(self, *, own=True):
        alpha = row(WORKER_INST, worker=W, agent_mxid=WORKER_AGENT, **({"dirs": "own"} if own else {}))
        beta = row(C.worker_instance_for("beta-fixture.net", W), worker=W,
                   agent_mxid="@reviewer:beta-fixture.net", server="beta-fixture.net",
                   gateway_url="https://chat.beta-fixture.net", owner_mxid="@owner:beta-fixture.net")
        self.write(row("alpha-fixture-net", primary=True, agent_mxid=CORE_AGENT), beta, alpha)
        return alpha, beta

    def test_all_lanes_and_aliases_share_one_legacy_home(self):
        alpha, beta = self.identities()
        with C._transaction(self.ws) as record:
            record["rows"][1]["launch_keys"] = ["AG2_REMOTE_TOKEN_REVIEWER_BETA"]
        expected = (self.own_root() / "tasks", self.own_root() / "results")
        self.assertEqual(C.worker_dirs(W, self.ws), expected)
        self.assertEqual(C.own_worker_homes(self.ws),
                         [{"worker": W, "tasks": expected[0], "results": expected[1]}])
        for lane in (alpha["instance"], beta["instance"], "reviewer_beta"):
            home = C.task_home(f"task-{lane}~abc", self.ws)
            self.assertEqual((home["tasks"], home["results"], home["own"]), (*expected, True))

    def test_primary_switch_identity_removal_and_rotation_keep_anchor(self):
        alpha, beta = self.identities()
        expected = C.worker_dirs(W, self.ws)
        with C._transaction(self.ws) as record:
            record["rows"].append(row("beta-fixture-net", server="beta-fixture.net"))
        C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual(C.worker_dirs(W, self.ws), expected)
        with C._transaction(self.ws) as record:
            record["rows"] = [r for r in record["rows"] if r["instance"] != alpha["instance"]]
            next(r for r in record["rows"] if r.get("worker") == W)["agent_mxid"] = "@replacement:beta-fixture.net"
        self.assertEqual(C.worker_dirs(W, self.ws), expected)
        self.assertEqual(C.worker_storage_root(W, self.ws), self.own_root())
        self.assertEqual(C.task_home(f"task-{beta['instance']}~abc", self.ws)["tasks"], expected[0])

    def test_removed_lane_tasks_still_resolve_in_the_workers_storage(self):
        alpha, beta = self.identities()
        tasks, results = C.ensure_worker_dirs(W, self.ws)
        task_id = f"task-{alpha['instance']}~abc"
        task = tasks / f"{task_id}.txt"
        task.write_text("pending")
        result = results / f"{task_id}.txt"
        result.write_text("finished")
        with C._transaction(self.ws) as record:
            record["rows"] = [r for r in record["rows"] if r["instance"] != alpha["instance"]]
        for location in (task, tasks / f"{task_id}.claimed-core-1.txt", tasks / "archive" / task.name):
            if task != location:
                task.rename(location)
                task = location
            with self.subTest(location=location.name):
                home = C.task_home(task_id, self.ws)
                self.assertEqual(home, {"lane": alpha["instance"], "worker": W, "tasks": tasks,
                                        "results": results, "own": True})
                self.assertEqual((home["results"] / result.name).read_text(), "finished")
        self.assertFalse(C.task_home(f"task-{alpha['instance']}~missing", self.ws)["own"])

    def test_one_toggle_controls_every_identity_and_preserves_other_home(self):
        alpha, beta = self.identities()
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertTrue(all(r.get("dirs") == "own" for r in C.worker_identities(W, self.ws)))
        expected = C.worker_dirs(W, self.ws)
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertTrue(all("dirs" not in r for r in C.worker_identities(W, self.ws)))
        self.assertEqual(C.task_other_home(f"task-{beta['instance']}~abc", self.ws),
                         {"tasks": expected[0], "results": expected[1]})
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertEqual(C.worker_dirs(W, self.ws), expected)

    def test_adding_an_identity_keeps_existing_files_in_the_same_inbox(self):
        self.standard(dirs="own")
        dirs = C.ensure_worker_dirs(W, self.ws)
        task_id = "task-0123abcd"
        (dirs[0] / f"{task_id}.txt").write_text("pending")
        (dirs[1] / f"{task_id}.txt").write_text("finished")
        before = C.record_path(self.ws).read_bytes()
        self.assertEqual(C.worker_dirs(W, self.ws), dirs)
        self.assertEqual(C.record_path(self.ws).read_bytes(), before, "folder reads do not write the record")
        with C._transaction(self.ws) as record:
            record["rows"].insert(0, row(C.worker_instance_for("beta-fixture.net", W), worker=W,
                                       agent_mxid="@reviewer:beta-fixture.net", server="beta-fixture.net"))
        self.assertEqual(C.worker_dirs(W, self.ws), dirs)
        self.assertEqual(C.task_home(task_id, self.ws)["tasks"], dirs[0])
        self.assertEqual((dirs[0] / f"{task_id}.txt").read_text(), "pending")
        self.assertEqual((dirs[1] / f"{task_id}.txt").read_text(), "finished")

    def test_multiple_shared_identities_do_not_require_a_server(self):
        self.identities(own=False)
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertEqual(C.own_worker_homes(self.ws), [])

    def test_storage_collision_is_refused_after_anchor_identity_is_removed(self):
        alpha, beta = self.identities()
        with C._transaction(self.ws) as record:
            record["rows"] = [r for r in record["rows"] if r["instance"] != alpha["instance"]]
            record["rows"].append(row("another-worker", worker=OTHER, agent_mxid=WORKER_AGENT))
        C.set_worker_dirs(W, False, workspace=self.ws)
        with self.assertRaises(C.ConnectionsError):
            C.set_worker_dirs(W, True, workspace=self.ws)
        with self.assertRaises(C.ConnectionsError):
            C.set_worker_dirs(OTHER, True, workspace=self.ws)

    def test_storage_schema_rejects_invalid_worker_anchor_and_setting(self):
        self.standard(dirs="own")
        path = C.record_path(self.ws)
        base = json.loads(path.read_text())
        for storage in ([], {"../worker": {"agent_mxid": WORKER_AGENT, "own": True}},
                        {W: {"agent_mxid": "", "own": True}},
                        {W: {"agent_mxid": WORKER_AGENT, "own": "own"}}):
            path.write_text(json.dumps({**base, "worker_storage": storage}))
            with self.assertRaises(C.SchemaError):
                C._read_record(path)


class RuntimeIdentity(Base):
    def test_shared_is_unchanged_and_own_differs(self):
        self.standard()
        shared = worker_lane.runtime_identity(W, WORKER_INST, self.ws)
        self.assertEqual(shared, "\0".join([W, WORKER_INST, "AG2_REMOTE_TOKEN_" + WORKER_INST.upper().replace("-", "_"),
                                            "https://chat.alpha-fixture.net", "alpha-fixture.net"]).encode())
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertEqual(worker_lane.runtime_identity(W, WORKER_INST, self.ws), shared + b"\0own")

    def test_the_declaration_root_is_the_own_dirs_root(self):
        self.standard(dirs="own")
        decl, why = worker_lane._declaration_for(W, self.ws)
        self.assertEqual(why, "")
        self.assertEqual(decl["env"]["AGENT_CONNECT_TASK_DIR"], str(C.worker_dirs(W, self.ws)[0]))
        self.assertEqual(decl["name"], connect_bridge.worker_name(self.own_root().name))


if __name__ == "__main__":
    unittest.main()
