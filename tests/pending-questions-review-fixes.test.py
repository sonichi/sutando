#!/usr/bin/env python3
"""The contracts the #5027 reviews asked for, each pinned against the production code:
an unreachable room is never a measured zero (every reader carries `unavailable`); an
answer or a close during an outage is recorded locally and replayed; the row key is
injective and the outbox entry goes only when that exact ask id's row is confirmed; a
failed outbox write stops the room write; outbox files cannot name a path outside the
outbox; reads do not write; and two skills declaring the store is a refusal.
No real room: the in-process store and the fake capability of the room-db suite."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
_spec = importlib.util.spec_from_file_location("rdb", REPO / "tests" / "pending-questions-room-db.test.py")
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)
pqs, pqa, adapter, reader, HOST = rdb.pqs, rdb.pqa, rdb.adapter, rdb.reader, rdb.HOST
skill_roots = importlib.import_module("skill_roots")
pqo = importlib.import_module("pending_questions_outbox")
SENT = rdb.SENT


class _Room(rdb._Ws):
    """A fake room with two rows already in it, as a host that has used the store before."""

    def setUp(self):
        super().setUp()
        rdb._install_fake_capability(self.ws)
        self.state = self.ws / "fake-room.json"
        os.environ["FAKE_ROOM_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_ROOM_STATE", None)
        self.store, _ = adapter.room_store(self.ws, environ={})
        self.a = self.ask("first?", store=self.store)
        self.b = self.ask("second?", store=self.store)
        self.assertEqual(adapter.count(self.ws)["open"], 2)

    def outage(self):
        return mock.patch.dict(os.environ, {"FAKE_ROOM_FAIL": "1"})


class TestOutageIsNeverZero(_Room):
    def test_the_adapter_and_the_reader_report_unknown_with_remote_rows_hidden(self):
        with self.outage():
            g = adapter.gather(self.ws, environ={})
            c = reader.count(self.ws, adapter=Path(adapter.__file__))
            r = reader.gather(self.ws, adapter=Path(adapter.__file__))
        self.assertEqual((g["unavailable"], g["done"], g["waiting"]), (True, None, []))
        self.assertIn("service refused", g["reason"])
        self.assertEqual((c["open"], c["done"], c["unavailable"]), (None, None, True))
        self.assertTrue(r["unavailable"])
        self.assertTrue(any("UNAVAILABLE" in n for n in r["notes"]), r["notes"])

    def test_an_adapter_that_raises_is_unknown_too(self):
        bad = self.ws / "raising_adapter.py"
        bad.write_text("def gather(ws):\n    raise ConnectionError('room down')\n")
        g = reader.gather(self.ws, adapter=bad)
        self.assertEqual((g["unavailable"], g["done"]), (True, None))
        self.assertIn("ConnectionError: room down", g["reason"])
        self.assertEqual(reader.count(self.ws, adapter=bad)["open"], None)

    def test_losing_the_capability_after_the_store_was_used_is_an_outage_not_an_empty_outbox(self):
        import shutil
        shutil.rmtree(self.ws / "skills")
        g = adapter.gather(self.ws, environ={})
        self.assertTrue(g["unavailable"])
        self.assertIn("used before", g["reason"])
        self.assertEqual(reader.gather(self.ws, skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))["unavailable"], True)

    def test_a_fresh_install_with_the_skill_but_no_room_measures_its_outbox_and_core_alone_measures_nothing(self):
        ws = Path(self.ws / "fresh")
        (ws / "state").mkdir(parents=True)
        g = reader.gather(ws, adapter=Path(adapter.__file__))
        self.assertEqual((g["unavailable"], g["done"], g["waiting"], g["pending_close"]), (False, 0, [], []))
        g = reader.gather(ws, skill_roots.declared(reader.DECLARATION, roots=ws / "no-skills"))
        self.assertEqual((g["unavailable"], g["done"], g["waiting"]), (True, None, []))
        self.assertIn("no skill declares one", g["reason"])
        self.assertEqual(reader.count(ws, skill_roots.declared(reader.DECLARATION, roots=ws / "no-skills"))["open"], None)

    def test_the_reminder_and_the_core_shim_say_unknown_and_send_nothing(self):
        cpq = rdb._cpq(self.ws)
        cpq.notify_macos = lambda count, titles: (_ for _ in ()).throw(AssertionError("must not notify"))
        with self.outage(), mock.patch.object(sys, "argv", ["x", "--notify", "--force"]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cpq.main(), 0)
        self.assertIn("UNKNOWN — room unreachable", out.getvalue())
        self.assertNotIn("Notified", out.getvalue())
        shim = REPO / "src" / "check-pending-questions.py"
        spec = importlib.util.spec_from_file_location("cpq_shim", shim)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        bad = self.ws / "raising_adapter.py"
        bad.write_text("def gather(ws):\n    raise ConnectionError('room down')\n")
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(["--store-adapter", str(bad)]), 0)
        self.assertIn("UNKNOWN", out.getvalue())
        self.assertNotIn("0 pending", out.getvalue())


class TestClosesSurviveAnOutage(_Room):
    def test_resolve_during_an_outage_records_locally_and_the_next_reconcile_applies_it(self):
        with self.outage():
            ok, msg = adapter.resolve(self.ws, self.a["ask_id"], "Answered")
        self.assertTrue(ok, msg)
        self.assertIn("recorded locally as Answered", msg)
        rec = json.loads((pqo.Outbox(self.ws).close_path(self.a["ask_id"])).read_text())
        self.assertEqual((rec["ask_id"], rec["status"]), (self.a["ask_id"], "Answered"))
        self.assertEqual(self.store.status_of(self.a["ask_id"]), "Open", "the room did not see it yet")
        self.assertEqual(adapter.count(self.ws)["open"], 1, "a locally closed row is no longer waiting")
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual((rec["closed"], rec["errors"]), ([self.a["ask_id"]], []))
        self.assertEqual(self.store.status_of(self.a["ask_id"]), "Answered")
        self.assertEqual(pqo.Outbox(self.ws).closes(), {})

    def test_the_reader_records_nothing_when_the_adapter_raises_and_the_adapter_names_unrecorded_when_it_cannot(self):
        bad = self.ws / "raising_adapter.py"
        bad.write_text("def resolve(ws, ask_id, status):\n    raise ConnectionError('room down')\n")
        ok, msg = reader.resolve(self.ws, self.a["ask_id"], "Answered", adapter=bad)
        self.assertFalse(ok)
        self.assertIn("adapter failed (ConnectionError: room down); nothing records the close", msg)
        self.assertEqual(pqo.Outbox(self.ws).closes(), {}, "core holds no close schema to record with")
        closed_dir = pqo.Outbox(self.ws).closed_dir
        closed_dir.write_text("a file where the directory should be")
        with self.outage():
            ok, msg = adapter.resolve(self.ws, self.a["ask_id"], "Answered")
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("UNRECORDED:"), msg)

    def test_a_store_that_refuses_is_not_changed_and_nothing_is_recorded(self):
        self.store.close(self.a["ask_id"], "Resolved")
        ok, msg = adapter.resolve(self.ws, self.a["ask_id"], "Answered")
        self.assertFalse(ok)
        self.assertIn("not changed", msg)
        self.assertEqual(pqo.Outbox(self.ws).closes(), {})
        ok, msg = adapter.resolve(self.ws, "ask-nope", "Answered")
        self.assertFalse(ok)
        self.assertEqual(pqo.Outbox(self.ws).closes(), {})

    def test_a_held_question_closed_before_it_reached_the_room_is_filed_then_closed(self):
        held = self.ask("held?")  # no store
        with self.outage():
            ok, msg = adapter.resolve(self.ws, held["ask_id"], "Resolved")
        self.assertTrue(ok, msg)
        self.assertIn("held in the outbox", msg)
        self.assertNotIn(held["ask_id"], [i["ask_id"] for i in adapter.gather(self.ws, environ={})["waiting"]],
                         "a locally closed held question is no longer waiting")
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual((rec["flushed"], rec["closed"], rec["errors"]), ([held["ask_id"]], [held["ask_id"]], []))
        self.assertEqual(self.store.status_of(held["ask_id"]), "Resolved")


class TestRowKeyAndOutboxIdentity(rdb._Ws):
    def test_the_row_key_is_injective_and_the_outbox_refuses_an_unsafe_id(self):
        self.assertNotEqual(pqs.row_id("ask/a"), pqs.row_id("ask?a"))
        self.assertEqual(pqs.row_id("ask-1"), "q-ask-1")
        self.assertTrue(pqs.row_id("ask/a").startswith("qh-"))
        self.assertNotEqual(pqs.row_id("h" + "0" * 64), pqs.row_id("x/y"), "a verbatim id never collides with a digest key")
        s = self.db()
        self.assertNotEqual(s._rid("ask/a"), s._rid("ask?a"))
        with self.assertRaises(pqo.BadAskId):
            pqs.Outbox(self.ws).save(pqs.Question("ask?a", "q"), SENT)
        with self.assertRaises(pqo.BadAskId):
            pqo.Outbox(self.ws).path("../core-status")

    def test_complete_requires_the_rows_own_ask_id_so_a_colliding_key_never_frees_another_entry(self):
        s = self.db()
        s.insert(pqs.Question("ask-a", "first"), SENT)
        s._keys["ask-b"] = s._rid("ask-a")  # a key collision, as a lossy row id would produce
        self.assertFalse(s.complete("ask-b"), "the row at that key carries ask-a, not ask-b")
        self.assertTrue(s.complete("ask-a"))
        pqs.Outbox(self.ws).save(pqs.Question("ask-b", "second"), SENT)
        flushed, errors = pqs.Outbox(self.ws).flush(s)
        self.assertEqual(flushed, [])
        self.assertIn("ask-b", errors[0])
        self.assertEqual(self.outbox(), ["ask-b.json"], "the only durable record of ask-b stays")
        self.assertEqual([(e["ask_id"], e["title"]) for e in s.entries()], [("ask-a", "first")])

    def test_where_names_the_real_row_key(self):
        s = self.db()
        self.assertIn(f"row {s._rid('ask-1')}", s.where("ask-1"))
        self.assertIn("~", s.where("ask-1"))


class TestOutboxFailureStopsTheRoomWrite(rdb._Ws):
    def test_no_outbox_record_no_row_and_the_report_says_nothing_holds_it(self):
        client = rdb.InProcClient()
        db = self.db(client)
        with mock.patch.object(pqo.Outbox, "save", side_effect=OSError("disk full")):
            out = adapter.ask_owner("q?", urgency="durable", workspace=self.ws, host=HOST, store=db)
        self.assertNotIn("add_row", client.calls, "the room write must not start without a held record")
        self.assertEqual(db.entries(), [])
        self.assertIsNone(out["record"])
        self.assertIsNone(out["outbox"])
        self.assertIn("disk full", out["db_error"])
        self.assertIn("NOT written", out["db_error"])
        self.assertIsNotNone(out["proactive_file"], "the owner is still asked")
        lines = "\n".join(adapter.report_lines(out))
        self.assertIn("recorded: FAILED", lines)
        self.assertIn("NOT recorded anywhere", lines)
        self.assertIn("sent: queued", lines)


class TestOutboxPathSafety(rdb._Ws):
    def _victim(self):
        v = self.ws / "state" / "core-status.json"
        v.write_text('{"status": "running"}')
        return v

    def test_a_record_naming_another_path_is_skipped_and_never_deleted_through(self):
        v = self._victim()
        ob = pqs.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        (ob.dir / "held.json").write_text(json.dumps({"question": {"ask_id": "../core-status", "question": "x"},
                                                      "sent": "**Sent:** x", "saved_at": "x"}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ob.entries(), [])
            self.assertEqual(ob.flush(self.db()), ([], []))
        self.assertIn("held.json unreadable", err.getvalue())
        self.assertTrue(v.exists(), "the victim is untouched")
        self.assertTrue((ob.dir / "held.json").exists(), "the odd file is left in place, never deleted")

    def test_a_symlink_in_the_outbox_is_neither_read_nor_unlinked_through(self):
        v = self._victim()
        ob = pqo.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        (ob.dir / "ask-evil.json").symlink_to(v)
        self.assertEqual(ob.entries(), [])
        ob.delete("ask-evil")
        self.assertTrue(v.exists())
        self.assertTrue((ob.dir / "ask-evil.json").is_symlink())

    def test_a_file_whose_stem_is_not_an_ask_id_is_skipped(self):
        ob = pqo.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        (ob.dir / "..json").write_text("{}")
        (ob.dir / "a b.json").write_text("{}")
        (ob.dir / "a:b.json").write_text(json.dumps({"question": {"ask_id": "a:b"}}))  # a file name, not an ask id
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ob.entries(), [])
        self.assertIn("..json is not a record name", err.getvalue())
        self.assertIn("a b.json is not a record name", err.getvalue())
        self.assertIn("a:b.json is not named by an ask id", err.getvalue())

    def test_a_close_record_is_validated_the_same_way(self):
        ob = pqo.Outbox(self.ws)
        ob.closed_dir.mkdir(parents=True)
        (ob.closed_dir / "ask-1.json").write_text(json.dumps({"ask_id": "ask-2", "status": "Resolved"}))
        (ob.closed_dir / "ask-3.json").write_text(json.dumps({"ask_id": "ask-3", "status": "Open"}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ob.closes(), {})
        with self.assertRaises(ValueError):
            ob.close("ask-1", "Open")


class TestReadsDoNotWrite(_Room):
    def test_gather_count_and_waiting_leave_the_outbox_the_room_and_the_file_alone(self):
        held = self.ask("held?")  # no store: in the outbox
        legacy = self.ws / "hosts" / HOST / "pending-questions.md"
        legacy.write_text("# Open\n\n## Prose question?\n\nbody\n\n")
        before = (self.state.read_text(), legacy.read_text(), self.outbox())
        g = adapter.gather(self.ws, environ={})
        adapter.count(self.ws)
        adapter.waiting(self.ws)
        reader.gather(self.ws, adapter=Path(adapter.__file__))
        self.assertEqual((self.state.read_text(), legacy.read_text(), self.outbox()), before)
        self.assertEqual([i["ask_id"] for i in g["waiting"] if not i["in_room"]], [held["ask_id"]])
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(rec["flushed"], [held["ask_id"]])
        self.assertEqual(len(rec["moved"]), 1)
        self.assertEqual(self.outbox(), [])
        self.assertIn("**Status:** moved", legacy.read_text())


if __name__ == "__main__":
    unittest.main()
