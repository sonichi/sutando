#!/usr/bin/env python3
"""Readers that find a task or its result by id look in the task's own home (`connections.task_home`).

A task an own-dir worker's lane queued (`dirs: "own"`) lives in `<ws>/agents/<segment>/{tasks,results}`;
every reader below finds it there, and finds a core task, a shared-dir worker's task and a plain id in the
shared tasks/ and results/ exactly as before:

- task_disposition (one id and the bulk form), result_publish's unrouted-cron check,
- agent-activity's phase line (task context, finished), stop interrupt (result state, ruling) and wedge
  detector (notice context, the result that ends an episode), and handoffs' pool child readiness.

Run: python3 tests/worker-own-dirs-readers.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ACTIVITY = ROOT / "skills" / "agent-activity" / "scripts"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "ag2-sparrow"), str(ACTIVITY)]
import connections as C, handoffs, result_publish, task_disposition as td, task_lookup  # noqa: E401,E402

import phase_line, stop_interrupt, wedge_detector  # noqa: E401,E402
from ag2_sparrow import agent_identity  # noqa: E402

W = "7c700e11" + "0" * 24
WORKER_AGENT = "@reviewer:alpha-fixture.net"
WORKER_INST = C.worker_instance_for("alpha-fixture.net", W)
OWN_ID, CORE_ID = f"task-{WORKER_INST}~abc", "task-1700000000000"


def row(instance, *, primary=False, **extra):
    return {"server": "alpha-fixture.net", "instance": instance, "gateway_url": "https://chat.alpha-fixture.net",
            "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": primary,
            "added_by": "owner", "added_at": "2026-11-16T00:00:00Z", "verified": True,
            "server_source": "gateway", **extra}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name).resolve()
        self.own = self.ws / "agents" / agent_identity.root_segment(WORKER_AGENT)
        for d in (self.own / "tasks", self.own / "results", self.ws / "tasks", self.ws / "results"):
            d.mkdir(parents=True)
        self.write(dirs="own")

    def write(self, **worker_extra):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "connections": [
            row("alpha-fixture-net", primary=True),
            row(WORKER_INST, worker=W, agent_mxid=WORKER_AGENT, **worker_extra)]}))

    def task(self, tasks: Path, tid: str, body="task: hi\n"):
        (tasks / f"{tid}.txt").write_text(f"id: {tid}\n{body}")

    def result(self, results: Path, tid: str, body="done\n"):
        (results / f"{tid}.txt").write_text(body)


class Disposition(Base):
    def test_own_task_is_found_in_its_folders(self):
        self.task(self.own / "tasks", OWN_ID)
        self.assertEqual(td.task_disposition(self.ws, OWN_ID), td.PENDING)
        self.result(self.own / "results", OWN_ID)
        self.assertEqual(td.task_disposition(self.ws, OWN_ID), td.ANSWERED)

    def test_shared_behaviour_is_unchanged(self):
        self.task(self.ws / "tasks", CORE_ID)
        self.assertEqual(td.task_disposition(self.ws, CORE_ID), td.PENDING)
        self.write()
        self.task(self.ws / "tasks", OWN_ID)
        self.assertEqual(td.task_disposition(self.ws, OWN_ID), td.PENDING)

    def test_bulk_groups_by_home(self):
        self.task(self.own / "tasks", OWN_ID)
        self.result(self.own / "results", OWN_ID)
        self.task(self.ws / "tasks", CORE_ID)
        self.assertEqual(td.task_dispositions(self.ws, [CORE_ID, OWN_ID, "bad id"]),
                         {CORE_ID: td.PENDING, OWN_ID: td.ANSWERED})


class HistoryAcrossTheSwitch(Base):
    """A lane's answers archived before a switch (own <-> shared) stay findable from the new home."""

    def archived(self, tasks: Path, results: Path, tid=OWN_ID):
        (tasks / "archive").mkdir(exist_ok=True)
        (results / "archive").mkdir(exist_ok=True)
        self.task(tasks / "archive", tid)
        self.result(results / "archive", tid)

    def assert_found(self):
        self.assertEqual(td.task_disposition(self.ws, OWN_ID), td.ANSWERED)
        self.assertEqual(td.task_dispositions(self.ws, [OWN_ID]), {OWN_ID: td.ANSWERED})
        self.assertEqual(td.task_disposition(self.ws, OWN_ID, unanswered=True), td.ARCHIVED)
        self.assertIsNotNone(task_lookup.find_task(self.ws, OWN_ID))
        self.assertTrue(handoffs._child_ready(self.ws, {"path": "pool", "child_ref": OWN_ID}))
        self.assertIsNotNone(task_lookup.find_result(self.ws, OWN_ID))

    def test_an_answer_archived_before_migration(self):
        self.archived(self.ws / "tasks", self.ws / "results")
        self.assert_found()

    def test_an_answer_archived_in_its_own_folders_before_undo(self):
        self.archived(self.own / "tasks", self.own / "results")
        self.write()
        self.assert_found()

    def test_a_stale_live_file_in_the_other_home_is_never_current(self):
        self.task(self.ws / "tasks", OWN_ID)
        self.result(self.ws / "results", OWN_ID)
        self.assertEqual(td.task_disposition(self.ws, OWN_ID), td.MISSING)
        self.assertIsNone(task_lookup.find_task(self.ws, OWN_ID))
        self.assertIsNone(task_lookup.find_result(self.ws, OWN_ID))

    def test_the_current_homes_archive_wins_over_the_other_homes(self):
        for tasks, results, body in ((self.own / "tasks", self.own / "results", "current"),
                                     (self.ws / "tasks", self.ws / "results", "other")):
            (tasks / "archive").mkdir(exist_ok=True)
            (results / "archive").mkdir(exist_ok=True)
            self.task(tasks / "archive", OWN_ID, f"task: {body}\n")
            self.result(results / "archive", OWN_ID, f"{body}\n")
        self.assertEqual(task_lookup.find_task(self.ws, OWN_ID).parent, self.own / "tasks" / "archive")
        self.assertEqual(task_lookup.find_result(self.ws, OWN_ID).read_text(), "current\n")
        self.assertEqual(task_lookup.find_ready_result(self.ws, OWN_ID).read_text(), "current\n")
        self.assertEqual([p.read_text() for p in task_lookup.result_candidates(self.ws, OWN_ID)],
                         ["current\n", "other\n"])

    def test_a_core_task_or_a_worker_without_an_agent_has_no_other_home(self):
        self.assertIsNone(C.task_other_home(CORE_ID, self.ws))
        self.assertIsNone(task_lookup.find_other_archived_ready_result(self.ws, CORE_ID))
        self.assertIsNone(task_lookup.find_other_archived_task(self.ws, CORE_ID))
        path = C.record_path(self.ws)
        rec = json.loads(path.read_text())
        rec["connections"][1].pop("agent_mxid")
        rec["connections"][1].pop("dirs")
        path.write_text(json.dumps(rec))
        self.assertIsNone(C.task_other_home(OWN_ID, self.ws))


class AProbeShapedIdInItsInbox(Base):
    """A plain id (no lane) whose task file is in the own inbox resolves to the own home throughout."""
    PROBE = "task-0123456789abcdef01"

    def test_before_and_after_it_is_answered_and_archived(self):
        self.task(self.own / "tasks", self.PROBE)
        self.assertEqual(td.task_disposition(self.ws, self.PROBE), td.PENDING)
        self.assertTrue(C.task_home(self.PROBE, self.ws)["own"])
        self.result(self.own / "results", self.PROBE)
        self.assertEqual(td.task_disposition(self.ws, self.PROBE), td.ANSWERED)
        self.assertTrue(handoffs._child_ready(self.ws, {"path": "pool", "child_ref": self.PROBE}))
        for sub in ("tasks", "results"):
            (self.own / sub / "archive").mkdir()
            (self.own / sub / f"{self.PROBE}.txt").rename(self.own / sub / "archive" / f"{self.PROBE}.txt")
        self.assertTrue(C.task_home(self.PROBE, self.ws)["own"])
        self.assertEqual(td.task_disposition(self.ws, self.PROBE), td.ANSWERED)
        self.assertEqual(td.task_dispositions(self.ws, [self.PROBE]), {self.PROBE: td.ANSWERED})
        self.assertIsNotNone(task_lookup.find_task(self.ws, self.PROBE))
        self.assertIsNotNone(task_lookup.find_result(self.ws, self.PROBE))


class UnroutedCron(Base):
    mod = result_publish

    def unrouted(self, target):
        with mock.patch.object(self.mod._helpers()[3], "resolve_workspace", return_value=self.ws):
            return self.mod._unrouted_cron_task(target)

    def test_a_cron_task_in_its_own_folders(self):
        self.task(self.own / "tasks", OWN_ID, "source: cron\ntask: hi\n")
        self.assertEqual(self.unrouted(self.own / "results" / f"{OWN_ID}.txt"), OWN_ID)
        self.assertEqual(self.unrouted(self.ws / "results" / f"{OWN_ID}.txt"), OWN_ID)
        self.assertIsNone(self.unrouted(self.ws / "notes" / f"{OWN_ID}.txt"))

    def test_shared_unchanged(self):
        self.task(self.ws / "tasks", CORE_ID, "source: cron\ntask: hi\n")
        self.assertEqual(self.unrouted(self.ws / "results" / f"{CORE_ID}.txt"), CORE_ID)
        self.assertIsNone(self.unrouted(self.own / "results" / f"{CORE_ID}.txt"))

    def test_without_the_engines_connections_it_is_the_shared_folders(self):
        with mock.patch.dict(sys.modules, {"connections": None}):
            home = result_publish._task_home(OWN_ID, self.ws)
        self.assertEqual(home, {"tasks": self.ws / "tasks", "results": self.ws / "results"})


class PackagedUnroutedCron(UnroutedCron):
    """The ag2-sparrow copy of result_publish, kept identical to src/."""
    from ag2_sparrow import result_publish as mod


class ActivityReaders(Base):
    def test_phase_line_reads_the_own_task_and_result(self):
        self.task(self.own / "tasks", OWN_ID, "access_tier: owner\nsender_name: Ann\ntask: fix it\n")
        goal, _people = phase_line._task_context(self.ws, OWN_ID)
        self.assertIn("fix it", goal or "")
        self.assertFalse(phase_line._finished(self.ws, OWN_ID))
        self.result(self.own / "results", OWN_ID)
        self.assertTrue(phase_line._finished(self.ws, OWN_ID))
        self.result(self.ws / "results", CORE_ID)
        self.assertTrue(phase_line._finished(self.ws, CORE_ID))

    def test_stop_interrupt_result_state_and_ruling(self):
        self.task(self.own / "tasks", OWN_ID)
        self.assertIsNone(stop_interrupt._result_state(self.ws, OWN_ID))
        self.result(self.own / "results", OWN_ID)
        self.assertEqual(stop_interrupt._result_state(self.ws, OWN_ID), "ready")
        (self.own / "results" / f"{OWN_ID}.txt").unlink()
        rec = {"target": OWN_ID, "control": CORE_ID, "at": time.time()}
        self.result(self.ws / "results", CORE_ID)
        self.assertIsNone(stop_interrupt.rule(self.ws, rec, time.time(), log=lambda m: None),
                          "an own-folder worker may still answer it")

    def test_stop_interrupt_core_task_is_still_ruled(self):
        self.task(self.ws / "tasks", "task-1700000000001")
        self.result(self.ws / "results", CORE_ID)
        rec = {"target": "task-1700000000001", "control": CORE_ID, "at": time.time()}
        with mock.patch.object(stop_interrupt, "_held_elsewhere", return_value=False), \
                mock.patch.object(stop_interrupt, "ActivityStore"):
            self.assertEqual(stop_interrupt.rule(self.ws, rec, time.time(), log=lambda m: None), "cancelled")

    def test_wedge_detector_context_and_episode_end(self):
        self.task(self.own / "tasks", OWN_ID, "source_room_id: !room:alpha-fixture.net\ntask: hi\n")
        self.assertIn("Room:", wedge_detector._notice_context(self.ws, OWN_ID))
        wedges = self.ws / "state" / "task-wedges"
        wedges.mkdir(parents=True)
        (wedges / f"{OWN_ID}.json").write_text("{}")
        self.result(self.own / "results", OWN_ID)
        self.assertIsNone(wedge_detector._status_for(self.ws, OWN_ID, None))
        self.assertFalse((wedges / f"{OWN_ID}.json").exists())

    def test_handoffs_pool_child_ready_in_its_folder(self):
        h = {"path": "pool", "child_ref": OWN_ID}
        self.assertFalse(handoffs._child_ready(self.ws, h))
        self.result(self.own / "results", OWN_ID)
        self.assertTrue(handoffs._child_ready(self.ws, h))
        self.result(self.ws / "results", CORE_ID)
        self.assertTrue(handoffs._child_ready(self.ws, {"path": "pool", "child_ref": CORE_ID}))


if __name__ == "__main__":
    unittest.main()
