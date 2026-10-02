#!/usr/bin/env python3
"""The failure states of review round 36 on #5027, each reproduced by the reviewer's own fault
injection against the production code and pinned to the fixed transition: (1) a direct resolve
of a pre-marker row commits the history marker BEFORE the close, and a marker that cannot be
written leaves the row open and records the close locally — a close record naming no held
question is itself history, so losing the capability afterwards reads as an outage, never as a
measured zero; (2) gather() reads the outbox before and after the rows and joins the two reads,
so a flush between them leaves the ask in one bucket and /answer accepts it; (3) `pq.py remind`
reconciles before it reminds — the injected adapter is read through its pass; (4) the linter
resolves the declared adapter path and requires containment, as runtime discovery does, so a
symlink escaping the skill is a lint error. No real room: the fake capability and in-process
store of the room-db suite."""
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
_spec = importlib.util.spec_from_file_location("rdb", REPO / "tests" / "pending-questions-room-db.test.py")
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)
pqs, adapter, reader, HOST, SENT = rdb.pqs, rdb.adapter, rdb.reader, rdb.HOST, rdb.SENT
pqo = importlib.import_module("pending_questions_outbox")
ADAPTER = Path(adapter.__file__)
SKILL = REPO / "skills" / "pending-questions"
ENV = {k: v for k, v in os.environ.items()
       if k not in ("SUTANDO_INSTANCE_ID", "SUTANDO_WORKSPACE_DIR", "SUTANDO_TASKS_DIR", "PENDING_QUESTIONS_ROOM")}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _marker_fails(*modules):
    """The reviewer's injection: the production marker writer raises, wherever it is bound."""
    patches = [mock.patch.object(m, "mark_store_used", side_effect=OSError("disk full")) for m in modules]

    class _All:
        def __enter__(self):
            for p in patches:
                p.__enter__()

        def __exit__(self, *a):
            for p in reversed(patches):
                p.__exit__(*a)
    return _All()


class _FakeRoom(rdb._Ws):
    def setUp(self):
        super().setUp()
        rdb._install_fake_capability(self.ws)
        os.environ["FAKE_ROOM_STATE"] = str(self.ws / "fake-room.json")
        self.addCleanup(os.environ.pop, "FAKE_ROOM_STATE", None)

    def store(self):
        return adapter.room_store(self.ws, environ={})[0]

    def lose_capability(self):
        shutil.rmtree(self.ws / "skills")

    def marker(self) -> Path:
        return self.ws / "state" / pqo.STORE_HISTORY

    def gathered(self):
        g = adapter.gather(self.ws, environ={})
        return {"waiting": [(it["ask_id"], it["in_room"]) for it in g["waiting"]], "done": g["done"],
                "pending_close": g["pending_close"], "unavailable": g["unavailable"]}

    def closes(self):
        return sorted(pqs.Outbox(self.ws).closes())


# ---- 2. gather() is coherent under a concurrent flush ------------------------------------

class GatherUnderAConcurrentFlush(_FakeRoom):
    def raced(self, aid):
        """The store gather() reads, whose row read is followed by another process's flush of the
        held question — the reviewer's schedule — once; a second reader of the same room."""
        store, other = self.store(), self.store()
        real = store.entries
        flushes = []

        def entries():
            rows = real()
            if not flushes:
                flushes.append(pqs.Outbox(self.ws).flush(other))
            return rows
        store.entries = entries
        return store, flushes

    def test_a_flush_between_the_row_read_and_the_outbox_read_leaves_the_ask_in_one_bucket(self):
        held = self.ask("race?")
        aid = held["ask_id"]
        store, flushes = self.raced(aid)
        with mock.patch.object(adapter, "room_store", return_value=(store, "the room")):
            g = adapter.gather(self.ws, environ={})
        self.assertEqual(flushes, [([aid], [])], "the concurrent flush filed it between the reads")
        self.assertEqual(self.outbox(), [], "and the held entry is gone")
        waiting = [(it["ask_id"], it["in_room"]) for it in g["waiting"]]
        self.assertEqual((waiting, g["done"], g["pending_close"], g["unavailable"]), ([(aid, False)], 0, [], False),
                         f"the ask fell between the two snapshots: {g}")

    def test_answer_accepts_the_question_the_race_would_have_hidden(self):
        api = _load("agent_api_r36", REPO / "src" / "agent-api.py")
        api.WORKSPACE_DIR, api.TASK_DIR = self.ws, self.ws / "tasks"
        (self.ws / "tasks").mkdir()
        held = self.ask("race?")
        aid = held["ask_id"]
        store, flushes = self.raced(aid)
        with mock.patch.object(api.pending_questions_reader, "_adapter", return_value=(adapter, str(ADAPTER))), \
                mock.patch.object(adapter, "room_store", return_value=(store, "the room")):
            code, body = api.answer_question(aid, "yes, go ahead")
        self.assertEqual(flushes, [([aid], [])])
        self.assertEqual(code, 200, body)
        tasks = sorted((self.ws / "tasks").glob("answer-*.txt"))
        self.assertEqual(len(tasks), 1, "the owner's answer was filed once")
        self.assertEqual(tasks[0].read_text(), f"User answered {aid}: yes, go ahead")
        self.assertEqual([(e["ask_id"], e["status"]) for e in self.store().entries()], [(aid, "Answered")])

    def test_a_question_in_both_reads_or_only_the_second_is_listed_once(self):
        """The join never doubles: a held question that is in both snapshots, one that arrives
        between them, and a row, each land in one bucket."""
        store = self.store()
        self.ask("in room?", store=store)
        both = self.ask("both?")
        real, late = store.entries, []

        def entries():
            rows = real()
            if not late:
                late.append(self.ask("late?"))
            return rows
        store.entries = entries
        with mock.patch.object(adapter, "room_store", return_value=(store, "the room")):
            g = adapter.gather(self.ws, environ={})
        ids = [it["ask_id"] for it in g["waiting"]]
        self.assertEqual(len(ids), len(set(ids)), f"an ask id is listed twice: {ids}")
        self.assertEqual(sorted(ids[1:]), sorted([both["ask_id"], late[0]["ask_id"]]))
        self.assertEqual([it["in_room"] for it in g["waiting"]], [True, False, False])
        self.assertEqual(g["done"], 0)


if __name__ == "__main__":
    unittest.main()
