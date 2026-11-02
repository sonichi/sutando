#!/usr/bin/env python3
"""The four coupled failure states of review round 34 on #5027, each reproduced by the
reviewer's own fault injection against the production code and pinned to the fixed
transition: (1) POST /answer publishes the answer task before it asks for the close, so a
failed write leaves the question open and a death after the write loses nothing; (2) a
confirmed room row marks the store as used — written, replayed, ingested or merely seen —
so losing the capability afterwards is an outage, never a measured zero, whatever became of
the introduction message; (3) a local close whose row the store view does not show is kept
and reported, never deleted on a missing row; (4) a local close stands in for a row only for
a held question or in outage mode, so an unknown id is refused and changes no count.
No real room: the fake capability and in-process store of the room-db suite."""
import http.server
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
_spec = importlib.util.spec_from_file_location("rdb", REPO / "tests" / "pending-questions-room-db.test.py")
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)
pqs, adapter, reader, HOST, SENT = rdb.pqs, rdb.adapter, rdb.reader, rdb.HOST, rdb.SENT
skill_roots = importlib.import_module("skill_roots")
pqo = importlib.import_module("pending_questions_outbox")
ADAPTER = Path(adapter.__file__)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _history(ws) -> bool:
    """The "room was used" fact as the adapter reads it, by whichever name this head gives it."""
    fn = getattr(pqo, "store_history", None) or getattr(pqo, "room_was_used")
    return fn(ws)


# ---- 1. /answer: the task first, the close only after it is durable --------------------

class AnswerOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = _load("agent_api_r34", REPO / "src" / "agent-api.py")
        cls.tmp = Path(tempfile.mkdtemp(prefix="r34-answer-"))
        (cls.tmp / "tasks").mkdir()
        cls.saved = (cls.api.WORKSPACE_DIR, cls.api.TASK_DIR, cls.api.API_TOKEN)
        cls.api.WORKSPACE_DIR, cls.api.TASK_DIR, cls.api.API_TOKEN = cls.tmp, cls.tmp / "tasks", "tok"
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), cls.api.Handler)
        cls.server.timeout = 0.5
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        cls.api.WORKSPACE_DIR, cls.api.TASK_DIR, cls.api.API_TOKEN = cls.saved

    def _raw(self, body):
        r = urllib.request.Request(f"{self.base}/answer", method="POST", data=json.dumps(body).encode())
        r.add_header("Authorization", "Bearer tok")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def post(self, body):
        out = {}
        t = threading.Thread(target=lambda: out.update(zip(("code", "data"), self._raw(body))), daemon=True)
        t.start()
        while t.is_alive():
            self.server.handle_request()
        t.join()
        return out["code"], out["data"]

    def _waiting(self, *ids):
        items = [{"id": i, "ask_id": i, "title": i, "snippet": "", "body": "", "asked_at": None,
                  "priority": None, "in_room": True} for i in ids]
        return {"waiting": items, "done": 0, "pending_close": [], "unavailable": False, "reason": None,
                "link": None, "notes": [], "store": None}

    def tasks(self):
        return sorted(p.name for p in (self.tmp / "tasks").glob("answer-*.txt"))

    def test_a_disk_full_task_write_through_the_real_handler_never_closes_the_question(self):
        """The reviewer's injection: the task write raises OSError('disk full')."""
        resolve_calls = []

        def _resolve(ws, ask_id, status, store=None):
            resolve_calls.append((ask_id, status))
            return True, "closed"
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("ask-crash")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve", _resolve), \
                mock.patch.object(Path, "write_text", side_effect=OSError("disk full")), \
                mock.patch("os.replace", side_effect=OSError("disk full")), \
                mock.patch("os.link", side_effect=OSError("disk full")):
            code, data = self.post({"id": "ask-crash", "answer": "keep me"})
        self.assertEqual(resolve_calls, [], f"http={code} {data}: the close was asked for with no durable answer")
        self.assertEqual(self.tasks(), [])
        self.assertGreaterEqual(code, 500, data)
        self.assertIn("stays open", data["error"])
        self.assertIn("disk full", data["error"])

    def test_the_close_is_asked_for_only_once_the_task_file_is_on_disk(self):
        seen_at_close = []

        def _resolve(ws, ask_id, status, store=None):
            seen_at_close.append(self.tasks())
            return True, "closed"
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("ask-order")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve", _resolve):
            code, data = self.post({"id": "ask-order", "answer": "ship it"})
        self.assertEqual(code, 200, data)
        self.assertEqual(len(seen_at_close), 1)
        self.assertEqual(len(seen_at_close[0]), 1, "the task was not on disk when the close was asked for")
        self.assertTrue(seen_at_close[0][0].startswith("answer-ask-order-"))
        self.assertEqual(list((self.tmp / "tasks").glob(".*")), [], "no temp file left beside it")
        for p in (self.tmp / "tasks").glob("answer-*.txt"):
            p.unlink()

    def test_a_death_between_the_task_and_the_close_keeps_the_answer_and_the_question(self):
        class Died(BaseException):
            pass
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("ask-die")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve", side_effect=Died()):
            with self.assertRaises(Died):
                self.api.answer_question("ask-die", "my word")
        [task] = self.tasks()
        self.assertTrue(task.startswith("answer-ask-die-"))
        self.assertIn("my word", (self.tmp / "tasks" / task).read_text())
        (self.tmp / "tasks" / task).unlink()

    def test_a_waiting_id_with_no_file_safe_characters_cannot_be_filed_and_stays_open(self):
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("///")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve") as r:
            code, data = self.api.answer_question("///", "x")
        self.assertEqual(code, 500, data)
        self.assertIn("no file-safe characters", data["error"])
        r.assert_not_called()
        self.assertEqual(self.tasks(), [])

    def test_an_id_that_is_not_waiting_files_no_task_and_an_unclosable_one_keeps_it(self):
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("ask-a")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve") as r:
            code, data = self.post({"id": "ask-zzz", "answer": "x"})
        self.assertEqual(code, 404, data)
        self.assertEqual(self.tasks(), [])
        r.assert_not_called()
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self._waiting("ask-a")), \
                mock.patch.object(self.api.pending_questions_reader, "resolve",
                                  return_value=(False, "not closed: no adapter; nothing records the close")):
            code, data = self.post({"id": "ask-a", "answer": "kept"})
        self.assertEqual(code, 503, data)
        self.assertTrue(data["recorded"])
        [task] = self.tasks()
        self.assertIn("kept", (self.tmp / "tasks" / task).read_text())
        (self.tmp / "tasks" / task).unlink()


# ---- 2. a confirmed row is store history, whatever became of the introduction ------------

class _FakeRoom(rdb._Ws):
    def setUp(self):
        super().setUp()
        rdb._install_fake_capability(self.ws)
        self.state = self.ws / "fake-room.json"
        os.environ["FAKE_ROOM_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_ROOM_STATE", None)

    def store(self):
        return adapter.room_store(self.ws, environ={})[0]

    def lose_capability(self):
        shutil.rmtree(self.ws / "skills")

    def assert_outage(self):
        g = adapter.gather(self.ws, environ={})
        self.assertEqual((g["unavailable"], g["done"]), (True, None), g)
        self.assertIn("used before", g["reason"])
        c = reader.count(self.ws, adapter=ADAPTER)
        self.assertEqual((c["open"], c["done"], c["unavailable"]), (None, None, True), c)
        self.assertTrue(reader.gather(self.ws, skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))["unavailable"],
                        "core alone never measures a zero either")


class StoreHistory(_FakeRoom):
    def test_a_row_written_while_results_is_not_a_directory_is_still_history(self):
        """The reviewer's injection: results/ unwritable as a directory, so the introduction
        message is never queued; the row lands all the same."""
        (self.ws / "results").rmdir()
        (self.ws / "results").write_text("not a directory")
        out = self.ask("q?", store=self.store())
        self.assertIn("FileExistsError", out["send_error"])
        self.assertEqual(len(self.store().entries()), 1)
        self.assertEqual(self.outbox(), [])
        self.assertFalse(pqo.room_introduced(self.ws) if hasattr(pqo, "room_introduced") else False,
                         "no introduction went out")
        self.assertTrue(_history(self.ws), "the confirmed row is the history, not the introduction")
        self.lose_capability()
        self.assert_outage()

    def test_a_row_replayed_by_reconcile_is_history(self):
        held = self.ask("held?")  # no store
        self.assertFalse(_history(self.ws))
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(rec["flushed"], [held["ask_id"]])
        self.assertTrue(_history(self.ws))
        self.lose_capability()
        self.assert_outage()

    def test_a_row_ingested_from_the_legacy_file_is_history(self):
        legacy = self.ws / "hosts" / HOST / "pending-questions.md"
        legacy.write_text("# Open\n\n## Prose question?\n\nbody\n\n")
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(len(rec["moved"]), 1, rec)
        self.assertTrue(_history(self.ws))
        self.lose_capability()
        self.assert_outage()

    def test_a_pre_existing_row_observed_by_a_reconcile_is_history(self):
        """An upgraded workspace: rows exist in the room, no marker was ever written here. A
        read leaves it that way (round 35: a read never writes); the explicit pass records it."""
        self.store().insert(self.q("ask-old", "from before?"), SENT)
        for marker in ("pending-questions-db-introduced", "pending-questions-store-history"):
            (self.ws / "state" / marker).unlink(missing_ok=True)
        self.assertFalse(_history(self.ws))
        self.assertEqual(adapter.count(self.ws)["open"], 1)
        self.assertFalse(_history(self.ws), "a read wrote the marker")
        self.assertEqual(adapter.reconcile_pass(self.ws, environ={})["errors"], [])
        self.assertTrue(_history(self.ws), "the reconcile saw a row of this workspace")
        self.lose_capability()
        self.assert_outage()

    def test_the_introduction_and_the_history_are_two_facts(self):
        out = self.ask("q?", store=self.store())
        self.assertIn("Pending questions database", (self.ws / "results" / out["proactive_file"]).read_text())
        self.assertTrue(pqo.room_introduced(self.ws))
        self.assertTrue(pqo.store_history(self.ws))
        (self.ws / "state" / pqo.STORE_HISTORY).unlink()
        self.assertTrue(pqo.store_history(self.ws), "a pre-split install's introduction mark still counts as history")
        (self.ws / "state" / pqo.ROOM_INTRODUCED).unlink()
        self.assertFalse(pqo.store_history(self.ws))
        out = self.ask("q2?", store=self.store())
        self.assertIn("Pending questions database", (self.ws / "results" / out["proactive_file"]).read_text(),
                      "the introduction is owed again, and only the introduction mark says so")


# ---- 3. a local close with no row in view is kept, never deleted -------------------------

class CloseReplay(rdb._Ws):
    def _stores(self):
        a = self.db(rdb.InProcClient(rdb.fake_client.FakeDoc()))
        b = self.db(rdb.InProcClient(rdb.fake_client.FakeDoc()))
        return a, b

    def test_replaying_a_real_close_against_an_empty_store_keeps_it_and_the_next_pass_applies_it(self):
        """The reviewer's injection: store A holds the open row, store B (empty, stale or the
        wrong store) is what the replay sees."""
        a, b = self._stores()
        a.insert(self.q("ask-real", "real?"), SENT)
        ob = pqs.Outbox(self.ws)
        ob.close("ask-real", "Answered")
        before = (a.status_of("ask-real"), sorted(ob.closes()))
        self.assertEqual(before, ("Open", ["ask-real"]))
        closed, errors = ob.replay_closes(b)
        self.assertEqual(closed, [], "an empty view proved nothing")
        self.assertEqual(errors, ["close ask-real: StoreError: no row for it in this store view; kept until one is seen"])
        self.assertEqual((a.status_of("ask-real"), sorted(ob.closes())), ("Open", ["ask-real"]), "the close survives")
        closed, errors = ob.replay_closes(a)
        self.assertEqual((closed, errors), (["ask-real"], []))
        self.assertEqual((a.status_of("ask-real"), sorted(ob.closes())), ("Answered", []))

    def test_a_row_seen_closed_already_retires_the_record_without_a_write(self):
        a, _ = self._stores()
        a.insert(self.q("ask-done", "done?"), SENT)
        a.close("ask-done", "Resolved")
        ob = pqs.Outbox(self.ws)
        ob.close("ask-done", "Answered")
        writes = len(a.client.calls)
        closed, errors = ob.replay_closes(a)
        self.assertEqual((closed, errors), (["ask-done"], []))
        self.assertEqual(ob.closes(), {})
        self.assertEqual(a.status_of("ask-done"), "Resolved", "the authoritative terminal row wins")
        self.assertNotIn("set_cells", a.client.calls[writes:])

    def test_a_kept_close_is_listed_and_counted_as_pending_close_not_done_or_open(self):
        rdb._install_fake_capability(self.ws)
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(self.ws / "fake-room.json")}):
            store = adapter.room_store(self.ws, environ={})[0]
            self.ask("in the room?", store=store)
            pqs.Outbox(self.ws).close("ask-elsewhere", "Resolved")
            adapter.reconcile_pass(self.ws, environ={})
            g = adapter.gather(self.ws, environ={})
            self.assertEqual((len(g["waiting"]), g["done"], g["pending_close"]), (1, 0, ["ask-elsewhere"]), g)
            self.assertTrue(any("await the row they name" in n for n in g["notes"]), g["notes"])
            c = adapter.count(self.ws)
            self.assertEqual((c["open"], c["done"], c["pending_close"]), (1, 0, 1))
            self.assertEqual(sorted(pqs.Outbox(self.ws).closes()), ["ask-elsewhere"], "still kept after the pass")


# ---- 4. a local close stands in for a row only when something says the row exists --------

class RecordNames(unittest.TestCase):
    def test_a_name_that_is_not_one_path_segment_is_refused_before_any_path_exists(self):
        import local_record
        d = local_record.RecordDir(Path(tempfile.mkdtemp()) / "records")
        for bad in ("../x", "a/b", ".hidden", "", "a b", "x" * 201, None):
            with self.assertRaises(local_record.BadName):
                d.path(bad)
        self.assertFalse(d.dir.exists())
        self.assertEqual(d.entries(), [])


class LocalCloseGate(rdb._Ws):
    def test_an_unknown_id_with_no_adapter_store_is_refused_and_changes_no_count(self):
        """The reviewer's injection: resolve an id that never existed on a clean install."""
        before = reader.count(self.ws, adapter=ADAPTER)
        self.assertEqual((before["open"], before["done"], before["unavailable"]), (0, 0, False))
        ok, msg = adapter.resolve(self.ws, "ask-never-existed", "Answered")
        self.assertFalse(ok, msg)
        self.assertIn("no held question ask-never-existed and no room row was ever confirmed here", msg)
        self.assertEqual(reader.count(self.ws, adapter=ADAPTER), before)
        self.assertFalse((self.ws / "state" / "pending-questions-outbox" / "closed").exists())

    def test_core_alone_records_no_close_and_says_so(self):
        ok, msg = reader.resolve(self.ws, "ask-never-existed", "Answered", skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))
        self.assertFalse(ok)
        self.assertIn("nothing records the close", msg)
        self.assertFalse((self.ws / "state" / "pending-questions-outbox").exists())

    def test_a_held_question_closes_locally_on_a_clean_install(self):
        held = self.ask("held?")
        ok, msg = adapter.resolve(self.ws, held["ask_id"], "Resolved")
        self.assertTrue(ok, msg)
        self.assertIn("held in the outbox", msg)
        c = adapter.count(self.ws)
        self.assertEqual((c["open"], c["done"], c["pending_close"]), (0, 1, 0))

    def test_an_unknown_id_closes_locally_only_in_outage_mode(self):
        pqo.mark_store_used(self.ws, "ask-earlier")
        ok, msg = adapter.resolve(self.ws, "ask-remote", "Answered")
        self.assertTrue(ok, msg)
        self.assertIn("outage mode", msg)
        g = adapter.gather(self.ws, environ={})
        self.assertTrue(g["unavailable"], "and the count stays unknown, not done 1")
        self.assertEqual(g["pending_close"], ["ask-remote"])


if __name__ == "__main__":
    unittest.main()
