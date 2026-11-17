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
