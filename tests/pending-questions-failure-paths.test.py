#!/usr/bin/env python3
"""The failure and fallback branches of the pending-questions contract, each pinned to what
the caller sees: an adapter that cannot load or reconcile, the reader and shim CLIs, a core
ask nothing could hold, outbox records that are not records, the legacy ingest's refusals
and its report, the store's option and replay refusals, the adapter's lookups and marks,
the reminder's quiet exits, and the briefing / mirror / ledger edges. No real room."""
import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SKILL = REPO / "skills" / "pending-questions" / "scripts"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SKILL))
_spec = importlib.util.spec_from_file_location("rdb", REPO / "tests" / "pending-questions-room-db.test.py")
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)
pqs, pqa, adapter, reader, HOST, SENT = rdb.pqs, rdb.pqa, rdb.adapter, rdb.reader, rdb.HOST, rdb.SENT
skill_roots = importlib.import_module("skill_roots")
pqo = importlib.import_module("pending_questions_outbox")
compat = importlib.import_module("pending_questions_compat")
ledger = importlib.import_module("pending_questions_ledger")
pq = importlib.import_module("pq")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _quiet():
    return contextlib.ExitStack()


class _Capture(contextlib.ExitStack):
    """stdout and stderr of the block, as .out / .err after it closes."""

    def __enter__(self):
        super().__enter__()
        self._o, self._e = io.StringIO(), io.StringIO()
        self.enter_context(contextlib.redirect_stdout(self._o))
        self.enter_context(contextlib.redirect_stderr(self._e))
        return self

    @property
    def out(self):
        return self._o.getvalue()

    @property
    def err(self):
        return self._e.getvalue()


class TestReaderDiscoveryAndCli(rdb._Ws):
    def _adapter_file(self, name, body):
        p = self.ws / name
        p.write_text(body)
        return p

    def test_an_unreadable_manifest_is_skipped_not_fatal(self):
        skills = self.ws / "skills-dir"
        (skills / "broken").mkdir(parents=True)
        (skills / "broken" / "manifest.json").write_text("{not json")
        self.assertEqual(skill_roots.declared_scripts(reader.DECLARATION, skills), [])
        self.assertIsNone(skill_roots.declared_script(reader.DECLARATION, skills))

    def test_an_adapter_that_fails_to_import_is_unavailable_with_the_error(self):
        bad = self._adapter_file("broken_adapter.py", "raise ImportError('needs pycrdt')\n")
        mod, why = reader._adapter(bad)
        self.assertIsNone(mod)
        self.assertIn("failed to load (ImportError: needs pycrdt)", why)
        g = reader.gather(self.ws, adapter=bad)
        self.assertTrue(g["unavailable"])
        self.assertIn("needs pycrdt", g["reason"])

    def test_reconcile_pass_reports_no_store_a_missing_pass_and_a_raising_one(self):
        rec = reader.reconcile_pass(self.ws, skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))
        self.assertEqual(rec["flushed"], [])
        self.assertIn("no store to reconcile with", rec["errors"][0])
        bare = self._adapter_file("bare.py", "def gather(ws):\n    return {'waiting': [], 'done': 0, 'notes': []}\n")
        self.assertIn("no store to reconcile with", reader.reconcile_pass(self.ws, adapter=bare)["errors"][0])
        raising = self._adapter_file("raising.py", "def reconcile_pass(ws):\n    raise ConnectionError('down')\n")
        self.assertEqual(reader.reconcile_pass(self.ws, adapter=raising)["errors"], ["adapter failed: ConnectionError: down"])
        ok = self._adapter_file("ok.py", "def reconcile_pass(ws):\n    return {'flushed': ['a'], 'moved': [], 'closed': [], 'errors': []}\n")
        self.assertEqual(reader.reconcile_pass(self.ws, adapter=ok)["flushed"], ["a"])

    def test_the_cli_counts_and_lists_held_questions_unknown_and_empty(self):
        held = pqa.ask_owner("held?", urgency="durable", workspace=self.ws, host=HOST)
        with mock.patch.object(skill_roots, "REPO_SKILLS", REPO / "skills"):  # the skill, no room capability
            with _Capture() as c:
                self.assertEqual(reader.main(["count", "--workspace", str(self.ws)]), 0)
            self.assertEqual(json.loads(c.out), {"open": 1, "done": 0, "pending_close": 0, "unavailable": False, "reason": None})
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--json", "--workspace", str(self.ws)]), 0)
            self.assertEqual([i["ask_id"] for i in json.loads(c.out)], [held["ask_id"]])
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--workspace", str(self.ws)]), 0)
            self.assertIn(f"- [{held['ask_id']}] held? (not yet in the room)", c.out)
            self.assertIn("room database: not used", c.err)
            pqo.Outbox(self.ws).close("ask-elsewhere", "Resolved")  # a close whose row is not in view
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--workspace", str(self.ws)]), 0)
            self.assertIn("- [ask-elsewhere] closed locally; its row is not in view yet", c.out)
            pqo.Outbox(self.ws).delete_close("ask-elsewhere")
            pqo.Outbox(self.ws).delete(held["ask_id"])
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--workspace", str(self.ws)]), 0)
            self.assertIn("0 pending questions", c.out)
            with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), _Capture() as c:
                self.assertEqual(reader.main(["count"]), 0)
            self.assertEqual(json.loads(c.out)["open"], 0, "no --workspace: the resolved one")
        with mock.patch.object(skill_roots, "REPO_SKILLS", self.ws / "no-skills"):
            with _Capture() as c:
                self.assertEqual(reader.main(["count", "--workspace", str(self.ws)]), 0)
            self.assertEqual(json.loads(c.out)["open"], None, "core alone measures nothing")
            self.assertIn("no skill declares one", c.err)
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--json", "--workspace", str(self.ws)]), 0)
            self.assertEqual(json.loads(c.out)["unavailable"], True)
            with _Capture() as c:
                self.assertEqual(reader.main(["list", "--workspace", str(self.ws)]), 0)
            self.assertIn("UNKNOWN — room unreachable", c.out)
            self.assertNotIn("0 pending", c.out)


class TestShimAndAskOwnerEntries(rdb._Ws):
    def _shim(self):
        return _load("cpq_shim_t", SRC / "check-pending-questions.py")

    def test_the_shim_hands_argv_to_an_adapter_with_a_reminder_else_lists(self):
        with_remind = self.ws / "with_remind.py"
        with_remind.write_text("def remind(argv, ws):\n    print('remind', argv, ws)\n    return 3\n")
        shim = self._shim()
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), _Capture() as c:
            self.assertEqual(shim.main(["--notify", "--store-adapter", str(with_remind)]), 3)
        self.assertIn(f"remind ['--notify', '--store-adapter', '{with_remind}'] {self.ws}", c.out)
        held = pqa.ask_owner("held?", urgency="durable", workspace=self.ws, host=HOST)
        listing = self.ws / "listing.py"
        listing.write_text("import pending_questions_outbox as o\n"
                           "def gather(ws):\n    return {'waiting': o.held_items(ws), 'done': 0, 'notes': ['n'], 'store': None}\n")
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), _Capture() as c:
            self.assertEqual(shim.main(["--notify", "--store-adapter", str(listing)]), 0)
        self.assertIn("1 pending questions; nothing sent (no store adapter with a reminder", c.out)
        self.assertIn(f"- [{held['ask_id']}] held? (not yet in the room)", c.out)
        self.assertIn("n", c.err)

    def test_ask_owner_says_not_recorded_when_nothing_could_hold_the_question(self):
        cli = _load("ask_owner_cli_t", REPO / "scripts" / "ask-owner.py")
        with mock.patch.object(skill_roots, "declared_script", return_value=Path(adapter.__file__)), \
                mock.patch.object(pqo.Outbox, "save", side_effect=OSError("disk full")), _Capture() as c:
            self.assertEqual(cli.main(["q?", "--urgency", "durable", "--workspace", str(self.ws)]), 0)
        self.assertIn("ask-owner: NOT RECORDED (outbox: OSError: disk full", c.err)
        self.assertIn("recorded: FAILED", c.out)
        self.assertIn("sent: queued", c.out)
        self.assertEqual(self.outbox(), [])
        no_ask = self.ws / "no_ask.py"
        no_ask.write_text("def gather(ws):\n    return {}\n")
        with _Capture() as c:
            self.assertEqual(cli.main(["q?", "--urgency", "durable", "--workspace", str(self.ws),
                                       "--store-adapter", str(no_ask)]), 0)
        self.assertIn("has no ask_owner", c.err)
        self.assertIn("recorded: NO STORE", c.out)


class TestOutboxRecords(rdb._Ws):
    def test_a_record_that_is_not_an_object_is_skipped(self):
        ob = pqo.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        ob.closed_dir.mkdir()
        (ob.dir / "ask-1.json").write_text(json.dumps({"question": "a string"}))
        (ob.closed_dir / "ask-2.json").write_text("[]")
        with _Capture() as c:
            self.assertEqual(ob.entries(), [])
            self.assertEqual(ob.closes(), {})
        self.assertIn("ask-1.json unreadable (not an object)", c.err)
        self.assertIn("ask-2.json unreadable (not an object)", c.err)

    def test_a_path_that_cannot_be_resolved_is_not_contained(self):
        ob = pqo.Outbox(self.ws)
        with mock.patch.object(Path, "resolve", side_effect=OSError("loop")):
            self.assertFalse(ob._held.contains(ob.dir / "ask-1.json"))

    def test_the_skills_outbox_skips_a_record_without_a_question_text(self):
        ob = pqs.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        (ob.dir / "ask-1.json").write_text(json.dumps({"question": {"ask_id": "ask-1"}, "sent": SENT, "saved_at": "x"}))
        with _Capture() as c:
            self.assertEqual(ob.entries(), [])
        self.assertIn("ask-1.json unreadable", c.err)
        self.assertTrue((ob.dir / "ask-1.json").exists())


class TestStoreRefusals(rdb._Ws):
    def test_an_unknown_priority_is_a_store_error_naming_the_options(self):
        with self.assertRaisesRegex(pqs.StoreError, "Priority has no option 'Urgent'; it has: High, Medium, Low"):
            self.db().insert_raw("ask-1", "q", "body", priority="Urgent")

    def test_the_script_client_forwards_every_op_and_names_a_bad_reply(self):
        script = self.ws / "echo_db.py"
        script.write_text("import json, sys\nreq = json.load(sys.stdin)\n"
                          "print(json.dumps({'ok': True, 'result': {'echo': req['op'], 'written': True}}))\n")
        c = pqs.ScriptDbClient([sys.executable, str(script)], timeout=30)
        self.assertIsNone(c.set_cells(pqs.DB_SCHEMA, "r", {"name": "x"}))
        self.assertIsNone(c.set_body(pqs.DB_SCHEMA, "r", "body"))
        self.assertEqual(c.guarded(pqs.DB_SCHEMA, "r", {}, {})["echo"], "guarded")
        script.write_text("print('not json')\n")
        with self.assertRaisesRegex(pqs.StoreError, "adapter exit 0"):
            c.row(pqs.DB_SCHEMA, "r")

    def test_replay_closes_waits_for_a_held_entrys_row_and_reports_other_failures(self):
        ob = pqs.Outbox(self.ws)
        ob.save(pqs.Question("ask-held", "held?"), SENT)
        ob.close("ask-held", "Resolved")
        ob.close("ask-gone", "Resolved")
        db = self.db()
        closed, errors = ob.replay_closes(db)
        self.assertEqual(closed, [], "a close with no row in view is kept, never dropped")
        self.assertEqual(errors, ["close ask-gone: StoreError: no row for it in this store view; kept until one is seen",
                                  "close ask-held: StoreError: its held entry has no row yet"])
        self.assertEqual(sorted(ob.closes()), ["ask-gone", "ask-held"])
        closed, errors = ob.replay_closes(self.db(rdb.InProcClient(fail="down")))
        self.assertEqual((closed, errors), ([], ["close ask-gone: StoreError: down", "close ask-held: StoreError: down"]))

    def test_write_question_reports_a_row_that_did_not_confirm(self):
        db = self.db()
        with mock.patch.object(db, "complete", return_value=False):
            w = pqs.write_question(pqs.Question("ask-1", "q?"), db, SENT)
        self.assertEqual((w.complete, w.db_error), (False, "the row was not confirmed complete"))

    def test_the_ledger_lock_is_released_even_when_the_transform_removed_it(self):
        lock = self.ws / "state" / "x.lock"
        err, res = ledger.under_lock(lock, lambda: (lock.rmdir(), "done")[1])
        self.assertEqual((err, res), (None, "done"))
        self.assertFalse(lock.exists())


class TestIngestRefusalsAndReport(rdb._Ws):
    def setUp(self):
        super().setUp()
        self.pq = self.ws / "hosts" / HOST / "pending-questions.md"

    def test_bad_fields_and_a_double_id_section_fall_back_or_are_skipped(self):
        self.assertIsNone(compat._question_from_fields("ask-1", "<!-- pq-fields: bm90IGpzb24= -->\n"))
        text = ("# Open\n\n## two ids?\n\nbody\n\n**Status:** open\n**Ask id:** ask-a\n**Ask id:** ask-b\n**Sent:** x\n\n"
                "## one id?\n\nbody\n\n**Status:** open\n**Ask id:** ask-c\n**Sent:** x\n\n")
        self.assertEqual([e["ask_id"] for e in compat.legacy_entries(text)], ["ask-c"])

    def test_marking_an_entry_that_is_no_longer_there_is_an_error_not_a_rewrite(self):
        self.pq.write_text("# Open\n\n## a?\n\nbody\n\n**Status:** open\n**Ask id:** ask-a\n**Sent:** x\n\n")
        before = self.pq.read_text()
        self.assertEqual(compat._mark_moved(self.pq, "ask-nope", "q-ask-nope"), "ask id 'ask-nope' names 0 entries, expected 1")
        self.assertEqual(self.pq.read_text(), before)

    def test_an_unreadable_file_an_unconfirmed_row_and_a_failed_mark_are_errors(self):
        self.pq.mkdir()
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, self.db())
        self.assertEqual(moved, [])
        self.assertIn("legacy file:", errors[0])
        self.pq.rmdir()
        self.pq.write_text("# Open\n\n## a?\n\nbody\n\n**Status:** open\n**Ask id:** ask-a\n**Sent:** x\n\n")
        db = self.db()
        with mock.patch.object(db, "complete", return_value=False):
            moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual((moved, errors), ([], ["legacy ask-a: StoreError: the row is still incomplete"]))
        self.assertIn("**Status:** open", self.pq.read_text())
        with mock.patch.object(compat, "_mark_moved", return_value="lock held by another writer"):
            moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual((moved, errors), ([], ["legacy ask-a: StoreError: lock held by another writer"]))
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), (["ask-a"], []))

    def test_the_report_sees_a_root_level_file_and_says_when_there_is_none(self):
        root = self.ws / "pending-questions.md"
        root.write_text("# Open\n")
        self.assertEqual([h for h, _, _ in compat.report(self.ws)], [HOST, ""])
        root.unlink()
        self.pq.unlink(missing_ok=True)
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), _Capture() as c:
            self.assertEqual(compat.main(["report"]), 0)
        self.assertIn("no legacy file on any host", c.out)


class TestAdapterLookupsAndMarks(rdb._Ws):
    def setUp(self):
        super().setUp()
        rdb._install_fake_capability(self.ws)
        self.state = self.ws / "fake-room.json"
        os.environ["FAKE_ROOM_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_ROOM_STATE", None)

    def test_a_missing_manifest_means_no_configured_room(self):
        with mock.patch.object(adapter, "HERE", self.ws / "nowhere" / "scripts"):
            self.assertEqual(adapter.configured_room(self.ws, {}), "")

    def test_a_link_is_a_convenience_never_a_precondition(self):
        with mock.patch.dict(sys.modules, {"room_collab": None}):
            self.assertIsNone(adapter._db_link(self.ws / "skills" / "room-collab" / "scripts", "!r:x", ""))

    def test_reconcile_pass_without_a_store_or_with_a_failing_one_is_an_error_not_a_raise(self):
        rec = adapter.reconcile_pass(Path(tempfile.mkdtemp()), environ={})
        self.assertIn("room database: not used", rec["errors"][0])
        with mock.patch.object(adapter, "reconcile_pending", side_effect=RuntimeError("boom")):
            self.assertEqual(adapter.reconcile_pass(self.ws, environ={})["errors"], ["RuntimeError: boom"])

    def test_resolve_closes_locally_when_the_held_entry_has_no_row_and_reports_an_unwritable_record(self):
        held = self.ask("held?")  # no store: the outbox only
        store, _ = adapter.room_store(self.ws, environ={})
        with mock.patch.object(adapter, "reconcile_pending", return_value={"flushed": [], "closed": [], "moved": [], "errors": []}):
            ok, msg = adapter.resolve(self.ws, held["ask_id"], "Resolved")
        self.assertTrue(ok, msg)
        self.assertIn("its held entry has no row yet", msg)
        closed_dir = pqo.Outbox(self.ws).closed_dir
        for p in closed_dir.iterdir():
            p.unlink()
        closed_dir.rmdir()
        closed_dir.write_text("not a directory")
        ok, msg = adapter._close_locally(self.ws, "ask-x", "Resolved", "why")
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("not recorded: no held question ask-x"), msg)
        pqo.mark_store_used(self.ws, "ask-earlier")  # outage mode: the write is attempted, and fails
        ok, msg = adapter._close_locally(self.ws, "ask-x", "Resolved", "why")
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("UNRECORDED: why; and the local close record failed"), msg)

    def test_the_ask_survives_a_failing_reconcile_and_an_unwritable_introduction_mark(self):
        store, _ = adapter.room_store(self.ws, environ={})
        mark = pqo.status_path(pqo.ROOM_INTRODUCED, self.ws)
        mark.mkdir()
        with mock.patch.object(adapter, "reconcile_pending", side_effect=RuntimeError("boom")), \
                mock.patch.object(adapter.core_ask, "notify_macos", return_value=(False, "no osascript")) as macos:
            out = adapter.ask_owner("q?", urgency="live", workspace=self.ws, host=HOST, store=store)
        self.assertEqual(out["reconcile"]["errors"], ["RuntimeError: boom"])
        self.assertIsNotNone(out["record"])
        self.assertIsNone(out["send_error"], "an introduction that was not needed writes no mark")
        self.assertEqual((out["macos"], out["macos_fix"]), (False, "no osascript"))
        macos.assert_called_once()
        mark.rmdir()
        with mock.patch.object(adapter, "mark_room_introduced", side_effect=OSError("read-only")):
            out = adapter.ask_owner("q2?", urgency="durable", workspace=self.ws, host=HOST, store=store)
        self.assertEqual(out["send_error"], "OSError: read-only")
        self.assertIsNotNone(out["record"], "the row still lands")
        with mock.patch.object(adapter, "mark_store_used", side_effect=OSError("read-only")):
            out = adapter.ask_owner("q3?", urgency="durable", workspace=self.ws, host=HOST, store=store)
        self.assertEqual(out["history_error"], "OSError: read-only")
        self.assertIsNotNone(out["record"], "the row still lands")
        self.assertIn("history: FAILED — OSError: read-only", "\n".join(adapter.report_lines(out)))

    def test_report_lines_name_every_reconcile_outcome(self):
        out = {"outbox": None, "record": "row", "heading": "## x — q?", "proactive_file": "p.txt", "where": "owner-dm",
               "reconcile": {"flushed": ["a"], "closed": ["b", "c"], "moved": ["d"], "errors": ["e"]}}
        lines = adapter.report_lines(out)
        self.assertIn("reconcile: FAILED — e", lines)
        self.assertIn("reconcile: filed 1 held question(s) from the outbox", lines)
        self.assertIn("reconcile: applied 2 local close(s)", lines)
        self.assertIn("reconcile: moved 1 legacy file entr(ies) into the database", lines)

    def test_remind_hands_argv_and_workspace_to_the_reminder(self):
        remind = importlib.import_module("pending_questions_remind")
        with mock.patch.object(remind, "main", return_value=7) as m:
            self.assertEqual(adapter.remind(["--notify"], self.ws), 7)
        m.assert_called_once_with(["--notify", "--store-adapter", adapter.__file__], self.ws, adapter=None)
        with mock.patch.object(remind, "main", return_value=7) as m:  # an injected one is kept
            adapter.remind(["--store-adapter", "/x/a.py"], self.ws)
        m.assert_called_once_with(["--store-adapter", "/x/a.py"], self.ws, adapter=None)
        one = reader.Resolved(adapter, adapter.__file__)  # the core entry's resolution: carried, no file injected
        with mock.patch.object(remind, "main", return_value=7) as m:
            self.assertEqual(adapter.remind(["--notify"], self.ws, resolved=one), 7)
        m.assert_called_once_with(["--notify"], self.ws, adapter=one)

    def test_ensure_writes_appends_missing_options_hidden_props_and_filters_only(self):
        schema = adapter.DB_SCHEMA
        have_prop = {"name": "Status", "type": "status", "options": [{"id": "open", "name": "Open", "color": "red"}], "order": 2048}
        have_view = {"name": "Board", "layout": "board", "hidden": ["ask_id"], "filter": [], "order": 1024}
        maps = {"dbs": {"pendingq": {}}, "props": {f"pendingq|{p['id']}": {"order": 1} for p in schema["props"]},
                "views": {f"pendingq|{v['id']}": {"order": 1, "hidden": [], "filter": []} for v in schema["views"]}}
        maps["props"]["pendingq|status"] = have_prop
        maps["props"]["pendingq|priority"] = {"order": 1, "options": list(schema["props"][2]["options"])}
        maps["views"]["pendingq|board"] = have_view
        w = adapter.ensure_writes(maps, schema, "agent", 1)
        self.assertEqual([o["id"] for o in w["props"]["pendingq|status"]["options"]], ["open", "answered", "resolved"])
        self.assertNotIn("pendingq|priority", w["props"], "a prop with its options is not rewritten")
        self.assertEqual(w["views"]["pendingq|board"]["hidden"], ["ask_id", "host", "recovery", "closed"])
        self.assertEqual(len(w["views"]["pendingq|board"]["filter"]), 2)
        self.assertEqual(adapter.ensure_writes({**maps, "props": {}, "views": {}}, schema, "a", 1).keys(), {"props", "views"})


class TestReminderQuietExits(rdb._Ws):
    def _cpq(self):
        cpq = rdb._cpq(self.ws)
        cpq.notify_macos = lambda count, titles: True
        return cpq

    def _fake(self, items, unavailable=False):
        fake = self.ws / "fake_adapter.py"
        fake.write_text("import json\n"
                        "def reconcile_pass(ws):\n    return {'flushed': [], 'moved': [], 'closed': [], 'errors': []}\n"
                        f"def gather(ws):\n    return json.loads({json.dumps(json.dumps({'waiting': items, 'done': 0, 'notes': [], 'store': 'f', 'unavailable': unavailable, 'reason': 'down' if unavailable else None}))})\n")
        return str(fake)

    def _item(self, ask_id="ask-1", title="Merge?"):
        return pqs.waiting_item(ask_id, title, f"body\n\n{SENT}", None, True)

    def test_voice_is_read_from_the_last_health_line(self):
        cpq = self._cpq()
        self.assertFalse(cpq.voice_client_connected(), "no log")
        cpq.VOICE_LOG.parent.mkdir(parents=True)
        cpq.VOICE_LOG.write_text("x\n[Health] client=true\n[Health] client=false\n")
        self.assertFalse(cpq.voice_client_connected())
        cpq.VOICE_LOG.write_text("[Health] client=false\n[Health] client=true\nnoise\n")
        self.assertTrue(cpq.voice_client_connected())
        cpq.VOICE_LOG.write_text("no health line\n")
        self.assertFalse(cpq.voice_client_connected())

    def test_delivery_speaks_when_voice_is_connected_and_folds_a_long_list(self):
        cpq = self._cpq()
        cpq.voice_client_connected = lambda: True
        qs = [self._item(f"ask-{i}", f"q{i}?") for i in range(7)]
        with _Capture():
            summary = cpq.deliver(qs, 7, [q["title"] for q in qs])
        self.assertIn("7 pending questions", summary)
        [voice] = list((self.ws / "results").glob("question-*.txt"))
        self.assertIn("You have 7 pending questions", voice.read_text())
        [dm] = list((self.ws / "results").glob("proactive-pending-q-*.txt"))
        self.assertIn("…and 2 more", dm.read_text())

    def test_the_workspace_argument_moves_every_path(self):
        cpq = self._cpq()
        other = Path(tempfile.mkdtemp(prefix="pq-other-"))
        (other / "results").mkdir()
        fake = self._fake([self._item()], unavailable=True)
        with _Capture() as c:
            self.assertEqual(cpq.main(["--notify", "--store-adapter", fake], workspace=other), 0)
        self.assertEqual((cpq.WORKSPACE, cpq.RESULTS_DIR), (other, other / "results"))
        self.assertIn("UNKNOWN — room unreachable (down); 1 held locally; nothing sent", c.out)
        self.assertIn("- [ask-1] Merge? (not yet in the room)", c.out)

    def test_nothing_to_remind_presenter_mode_and_the_cooldown_each_send_nothing(self):
        cpq = self._cpq()
        with _Capture() as c:
            self.assertEqual(cpq.main(["--notify", "--store-adapter", self._fake([])]), 0)
        self.assertIn("0 pending questions — nothing to remind", c.out)
        fake = self._fake([self._item()])
        sentinel = self.ws / "state" / "presenter-mode.sentinel"
        sentinel.write_text("9999-01-01T00:00:00Z")
        with _Capture() as c:
            self.assertEqual(cpq.main(["--notify", "--store-adapter", fake]), 0)
        self.assertIn("(presenter-mode) 1 pending questions — suppressed", c.out)
        sentinel.unlink()
        cpq.write_notify_stamp([self._item()])
        with _Capture() as c:
            self.assertEqual(cpq.main(["--notify", "--store-adapter", fake]), 0)
        self.assertIn("(cooldown) 1 pending questions — skipping notification", c.out)
        self.assertEqual(list((self.ws / "results").glob("proactive-pending-q-*.txt")), [])


class TestPqCliEdges(rdb._Ws):
    def test_list_prints_a_snippet_that_differs_from_the_title_and_resolves_the_workspace(self):
        item = pqs.waiting_item("ask-1", "Merge?", f"CI is green\n\n{SENT}", None, True)
        g = {"waiting": [item], "done": 0, "unavailable": False, "reason": None, "link": None, "notes": [], "store": "s"}
        with mock.patch.object(adapter, "gather", return_value=g), \
                mock.patch("workspace_default.resolve_workspace", return_value=self.ws) as rw, _Capture() as c:
            self.assertEqual(pq.main(["list"]), 0)
        rw.assert_called_once()
        self.assertIn("- [ask-1] Merge?\n    CI is green", c.out)


class TestBriefingAndMirrorEdges(unittest.TestCase):
    def test_the_briefing_summary_and_line_accept_a_count_or_a_bare_list(self):
        mb = _load("mb_t", SRC / "morning-briefing.py")
        self.assertEqual(mb.pending_summary({"count": None}), "unknown (room unreachable)")
        self.assertEqual(mb.pending_summary(["a", "b"]), "2")
        self.assertIn("2 pending questions are waiting", mb.pending_line(["a", "b"]))
        self.assertIsNone(mb.pending_line([]))

    def test_a_sweep_counts_the_asks_mirror(self):
        om = _load("om_t3", SRC / "obsidian-mirror.py")
        vault, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        g = {"waiting": [pqs.waiting_item("ask-1", "Merge?", "b", None, True)], "done": 0, "unavailable": False,
             "reason": None, "link": None, "notes": [], "store": "s"}
        with mock.patch.object(om.pending_questions_reader, "gather", return_value=g):
            self.assertEqual(om.sweep(vault, ws)["asks"], 1)
            self.assertEqual(om.sweep(vault, ws)["asks"], 0, "unchanged: not written again")


if __name__ == "__main__":
    unittest.main()
