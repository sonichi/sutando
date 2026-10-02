#!/usr/bin/env python3
"""skills/pending-questions/scripts/pq.py: each verb delegates to the existing
owner (ask-owner, the reminder's gather, FileStore/RoomDbStore, the reminder), the
skill's adapter is injected at this edge, and core finds it only by its manifest
declaration. No real room or workspace is touched: a temp workspace, the fake
room-collab capability and the in-process database store of the room-db test."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills" / "pending-questions"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rdb = _load("pq_room_db_test", REPO / "tests" / "pending-questions-room-db.test.py")
pq = _load("pq_cli", SKILL / "scripts" / "pq.py")
pqs, pqa, HOST, ROOM = rdb.pqs, rdb.pqa, rdb.HOST, rdb.ROOM


class _Ws(rdb._Ws):
    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pq.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def ask(self, question, store=None):
        return pqa.ask_owner(question, urgency="durable", workspace=self.ws, host=HOST, store=store)


class TestAsk(_Ws):
    def test_ask_records_and_injects_the_skill_adapter(self):
        rdb._install_fake_capability(self.ws)
        state = self.ws / "fake-room.json"
        rc, out, _ = self.cli("ask", "Merge #12?", "--default-action", "merge", "--option", "Hold=wait",
                              "--urgency", "durable", "--workspace", str(self.ws),
                              env={"FAKE_ROOM_STATE": str(state)})
        self.assertEqual(rc, 0)
        self.assertIn("Pending questions database", out)
        self.assertIn("**Status:** open", self.pq.read_text())
        [body] = json.loads(state.read_text())["bodies"].values()
        self.assertIn("**Approve** -> merge\n**Hold** -> wait", body)
        self.assertEqual(pqs.registered_adapter(self.ws), str(pq.ADAPTER))

    def test_ask_without_the_capability_keeps_the_file(self):
        rc, out, _ = self.cli("ask", "q?", "--urgency", "durable", "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn("room database: not used (no room-collab capability installed)", out)
        self.assertEqual(len(self.pq.read_text().split("**Ask id:**")), 2)

    def test_ask_delegates_to_ask_owner(self):
        seen = {}

        def _ask(*a, **kw):
            seen.update(kw, question=a[0])
            return {"db_error": None, "ledger": "x", "heading": "## x", "ledger_error": None,
                    "proactive_file": "p", "where": "w", "send_error": None, "macos": None}
        with mock.patch.object(pqa, "ask_owner", _ask):
            self.cli("ask", "q?", "--context", "why", "--workspace", str(self.ws))
        self.assertEqual((seen["question"], seen["context"]), ("q?", "why"))


class TestList(_Ws):
    def test_list_is_the_reminders_waiting_set_with_ask_ids(self):
        a, b = self.ask("first?"), self.ask("second?")
        pqs.FileStore(self.pq).set_status(b["ask_id"], "Resolved")
        rc, out, _ = self.cli("list", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([(i["ask_id"], i["title"]) for i in json.loads(out)],
                         [(a["ask_id"], a["heading"][3:])])
        rc, out, _ = self.cli("list")
        self.assertIn(f"1 waiting on the owner:\n- [{a['ask_id']}] {a['heading'][3:]}", out)

    def test_list_reads_the_database_and_heals_the_file(self):
        db = self.db()
        a = self.ask("first?", store=db)
        db.close(a["ask_id"], "Answered")
        with mock.patch.object(pq, "_room_store", return_value=(db, ROOM)):
            rc, out, _ = self.cli("list", "--json")
        self.assertEqual(json.loads(out), [])
        self.assertIn("**Status:** answered — in the room database", self.pq.read_text())

    def test_an_empty_list_says_why(self):
        rc, out, _ = self.cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("pending questions", out)


class TestResolve(_Ws):
    def test_resolve_closes_the_file_and_the_row(self):
        db = self.db()
        a = self.ask("first?", store=db)
        with mock.patch.object(pq, "_room_store", return_value=(db, ROOM)):
            rc, out, _ = self.cli("resolve", a["ask_id"], "--answered")
        self.assertEqual(rc, 0, out)
        self.assertIn("**Status:** answered", self.pq.read_text())
        self.assertEqual(db.status_of(a["ask_id"]), "Answered")

    def test_resolve_never_reopens_a_closed_row(self):
        db = self.db()
        a = self.ask("first?", store=db)
        db.close(a["ask_id"], "Answered")
        with mock.patch.object(pq, "_room_store", return_value=(db, ROOM)):
            rc, out, _ = self.cli("resolve", a["ask_id"])
        self.assertEqual(rc, 0)
        self.assertIn("room database: not changed", out)
        self.assertEqual(db.status_of(a["ask_id"]), "Answered")
        self.assertIn("**Status:** resolved", self.pq.read_text())

    def test_resolve_file_only_and_an_unknown_id(self):
        a = self.ask("first?")
        rc, out, _ = self.cli("resolve", a["ask_id"], "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn("room database: not used", out)
        self.assertEqual(pqs.FileStore(self.pq).open_entries(), [])
        rc, out, _ = self.cli("resolve", "ask-nope", "--workspace", str(self.ws))
        self.assertEqual(rc, 1)
        self.assertIn("file: not changed", out)


class TestRemind(unittest.TestCase):
    def _argv(self, *args):
        with mock.patch.object(pq.subprocess, "run") as run:
            run.return_value.returncode = 3
            self.assertEqual(pq.main(["remind", *args]), 3)
        return run.call_args[0][0]

    def test_remind_passes_through_with_the_adapter_injected(self):
        argv = self._argv("--force")
        self.assertEqual(argv, [sys.executable, str(REPO / "src" / "check-pending-questions.py"),
                                "--force", "--store-adapter", str(pq.ADAPTER)])
        self.assertEqual(self._argv("--store-adapter", "x.py")[2:], ["--store-adapter", "x.py"])

    def test_an_unknown_verb_prints_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pq.main(["nope"]), 2)
        self.assertIn("pq.py ask", err.getvalue())


class TestDeclaration(_Ws):
    def _skill(self, name, manifest, script="scripts/a.py"):
        d = self.ws / "skills-dir" / name
        (d / "scripts").mkdir(parents=True)
        (d / script).write_text("def room_store(ws):\n    return None, 'fake'\n")
        (d / "manifest.json").write_text(json.dumps(manifest))
        return d

    def test_the_repo_skill_declares_its_adapter(self):
        self.assertEqual(pqs.declared_adapter(REPO / "skills"), pq.ADAPTER)

    def test_a_declaration_must_stay_inside_its_skill(self):
        skills = self.ws / "skills-dir"
        self._skill("a-off", {"enabled": False, "pending_questions_store": "scripts/a.py"})
        self._skill("b-out", {"pending_questions_store": "../a-off/scripts/a.py"})
        self.assertIsNone(pqs.declared_adapter(skills))
        good = self._skill("c-ok", {"pending_questions_store": "scripts/a.py"})
        self.assertEqual(pqs.declared_adapter(skills), (good / "scripts" / "a.py").resolve())
        self.assertEqual(pqs.load_adapter_store(pqs.declared_adapter(skills), self.ws), (None, "fake"))
        self.assertEqual(pqs.load_adapter_store(None, self.ws)[0], None)

    def test_without_the_skill_the_reminder_reminds_from_the_file(self):
        self.pq.write_text("## still waiting\n\nbody\n")
        cpq = rdb._cpq(self.pq, self.ws)
        cpq.SKILLS_DIR = self.ws / "no-skills"
        with mock.patch.object(cpq, "deliver", return_value="Notified: 1") as deliver, \
                mock.patch.object(cpq, "load_store") as load, \
                mock.patch.object(sys, "argv", ["check-pending-questions.py"]), \
                contextlib.redirect_stdout(io.StringIO()):
            cpq.main()
        self.assertEqual((deliver.call_count, load.call_count), (1, 0))

    def test_without_the_skill_ask_owner_keeps_the_file(self):
        cli = REPO / "scripts" / "ask-owner.py"
        with mock.patch.object(pqs, "declared_adapter", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = _load("ask_owner_cli", cli).main(["q?", "--urgency", "durable", "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        self.assertIn("room database: not used (no pending-questions store adapter installed)", out.getvalue())
        self.assertIn("**Status:** open", self.pq.read_text())


class TestSharedRoom(_Ws):
    SHARED = "!shared:test.invalid"

    def store_room(self, env):
        rdb._install_fake_capability(self.ws)
        store, where = rdb.adapter.room_store(self.ws, environ=env)
        return store, where

    def test_the_owner_dm_is_the_default(self):
        store, room = self.store_room({})
        self.assertEqual(room, ROOM)
        self.assertIn("DM room", store.label)

    def test_a_host_state_file_moves_the_database_to_the_shared_room(self):
        (self.ws / "state" / "pending-questions-room").write_text(self.SHARED + "\n")
        store, room = self.store_room({})
        self.assertEqual(room, self.SHARED)
        self.assertNotIn("DM", store.label)

    def test_env_wins_over_the_state_file(self):
        (self.ws / "state" / "pending-questions-room").write_text("!fromstate:test.invalid")
        self.assertEqual(self.store_room({"PENDING_QUESTIONS_ROOM": self.SHARED})[1], self.SHARED)

    def test_the_manifest_declares_the_key_empty(self):
        cfg = json.loads((SKILL / "manifest.json").read_text())
        self.assertEqual(cfg["config"], {"PENDING_QUESTIONS_ROOM": ""})


class TestFirstAskIntroduction(_Ws):
    def test_the_first_ask_with_a_database_says_where_it_lives_and_only_once(self):
        db = rdb.pqs.RoomDbStore(rdb.InProcClient(), lock=self.ws / "state" / "l", host=HOST)
        bodies = []
        for q in ("First?", "Second?"):
            out = pqa.ask_owner(q, urgency="durable", workspace=self.ws, host=HOST, store=db)
            bodies.append((self.ws / "results" / out["proactive_file"]).read_text())
        self.assertIn("Pending questions database", bodies[0])
        self.assertIn("not on a schedule", bodies[0])
        self.assertNotIn("Pending questions database", bodies[1])

    def test_no_database_no_introduction(self):
        out = pqa.ask_owner("Solo?", urgency="durable", workspace=self.ws, host=HOST)
        self.assertNotIn("Pending questions database", (self.ws / "results" / out["proactive_file"]).read_text())

    def test_the_new_install_template_schedules_reconciliation_but_no_reminder(self):
        crons = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
        runs = [json.dumps(c) for c in crons if "check-pending-questions" in json.dumps(c)]
        self.assertTrue(runs)  # the silent reconciliation stays scheduled
        self.assertTrue(all("--reconcile-only" in r for r in runs))  # nothing scheduled ever reminds

if __name__ == "__main__":
    unittest.main()
