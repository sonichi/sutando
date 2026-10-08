#!/usr/bin/env python3
"""skills/pending-questions/scripts/pq.py: each verb delegates to the existing owner
(ask-owner with the adapter injected, the sibling adapter's read-only gather / explicit
reconcile / resolve, the reminder with --notify), and core finds the adapter only by its
manifest declaration — refusing when two skills declare it. No real room or workspace is
touched: a temp workspace, the fake room-collab capability and the in-process database
store of the room-db test."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
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
pqs, pqa, adapter, reader, HOST, ROOM = rdb.pqs, rdb.pqa, rdb.adapter, rdb.reader, rdb.HOST, rdb.ROOM
skill_roots = importlib.import_module("skill_roots")


class _Ws(rdb._Ws):
    def setUp(self):
        super().setUp()
        rdb._install_fake_capability(self.ws)
        self.state = self.ws / "fake-room.json"
        os.environ["FAKE_ROOM_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_ROOM_STATE", None)

    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(self.state), **(env or {})}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pq.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def store(self):
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(self.state)}):
            return adapter.room_store(self.ws, environ={})[0]


class TestAsk(_Ws):
    def test_ask_records_the_row_through_the_skill_adapter(self):
        rc, out, _ = self.cli("ask", "Merge #12?", "--default-action", "merge", "--option", "Hold=wait",
                              "--urgency", "durable", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        self.assertIn("recorded: Pending questions database", out)
        self.assertIn(f"row: https://collab.test.invalid/#/room/{ROOM}?surface=db&page=pendingq", out)
        [body] = json.loads(self.state.read_text())["bodies"].values()
        self.assertIn("**Approve** -> merge\n**Hold** -> wait", body)
        self.assertIn("**Sent:** queued owner-dm", body)
        self.assertEqual(self.outbox(), [])

    def test_ask_without_the_capability_holds_the_question_in_the_outbox(self):
        shutil.rmtree(self.ws / "skills")
        rc, out, err = self.cli("ask", "q?", "--urgency", "durable", "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn("room database: not used (no room capability installed", out)
        self.assertIn("recorded: OUTBOX", out)
        self.assertIn("ROOM DATABASE WRITE FAILED", err)
        self.assertEqual(len(self.outbox()), 1)

    def test_ask_delegates_to_the_adapters_ask_owner_with_its_store(self):
        seen = {}

        def _ask(question, **kw):
            seen.update(kw, question=question)
            return {"db_error": None, "record": "x", "heading": "## x", "outbox": None, "link": None,
                    "proactive_file": "p", "where": "w", "send_error": None, "macos": None, "reconcile": None}
        with mock.patch.object(reader, "load_adapter", return_value=adapter), mock.patch.object(adapter, "ask_owner", _ask):
            self.cli("ask", "q?", "--context", "why", "--workspace", str(self.ws))
        self.assertEqual((seen["question"], seen["context"]), ("q?", "why"))
        self.assertIsInstance(seen["store"], pqs.RoomDbStore)


class TestList(_Ws):
    def test_list_is_this_hosts_open_rows_plus_the_outbox_once_each(self):
        store = self.store()
        a, b = self.ask("first?", store), self.ask("second?", store)
        store.close(b["ask_id"], "Resolved")
        rc, out, err = self.cli("list", "--json", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, err)
        self.assertEqual([(i["ask_id"], i["title"], i["in_room"]) for i in json.loads(out)],
                         [(a["ask_id"], "first?", True)])
        rc, out, _ = self.cli("list", "--workspace", str(self.ws))
        self.assertIn(f"1 waiting on the owner:\n- [{a['ask_id']}] first?", out)

    def test_list_is_read_only_and_reconcile_is_the_pass_that_files_a_held_question(self):
        held = self.ask("held?")  # no store

        def room():
            return json.loads(self.state.read_text()) if self.state.exists() else {}
        rc, out, _ = self.cli("list", "--workspace", str(self.ws))
        self.assertIn(f"- [{held['ask_id']}] held? (not yet in the room)", out)
        self.assertEqual(self.outbox(), [f"{held['ask_id']}.json"], "list files nothing")
        self.assertEqual((room().get("rows"), room().get("dbs")), ({}, {}), "list writes no row, not even the database")
        rc, out, _ = self.cli("reconcile", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        self.assertIn("filed 1 held", out)
        self.assertEqual(self.outbox(), [])
        rc, out, _ = self.cli("list", "--json", "--workspace", str(self.ws))
        [item] = json.loads(out)
        self.assertEqual((item["ask_id"], item["in_room"]), (held["ask_id"], True))

    def test_an_unreachable_room_lists_unknown_never_zero(self):
        held = self.ask("held?")
        with mock.patch.dict(os.environ, {"FAKE_ROOM_FAIL": "1"}):
            rc, out, err = self.cli("list", "--workspace", str(self.ws))
            self.assertEqual(rc, 0)
            self.assertIn("pending questions: UNKNOWN — room unreachable", out)
            self.assertIn(f"- [{held['ask_id']}] held? (not yet in the room)", out)
            self.assertNotIn("0 pending", out)
            self.assertIn("service refused", err)
            rc, out, _ = self.cli("list", "--json", "--workspace", str(self.ws))
            j = json.loads(out)
            self.assertEqual((j["unavailable"], [i["ask_id"] for i in j["waiting"]]), (True, [held["ask_id"]]))
            rc, out, _ = self.cli("reconcile", "--workspace", str(self.ws))
            self.assertEqual(rc, 1)
            self.assertIn("reconcile: FAILED", out)

    def test_the_count_agrees_with_the_list(self):
        store = self.store()
        for q in ("a?", "b?", "c?"):
            self.ask(q, store)
        self.ask("held?", self.db(rdb.InProcClient(fail="down")))
        rc, out, _ = self.cli("list", "--json", "--workspace", str(self.ws))
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(self.state)}):
            self.assertEqual(adapter.count(self.ws)["open"], len(json.loads(out)))
            self.assertEqual(adapter.count(self.ws)["open"], 4)

    def test_an_empty_list_says_where_it_looked(self):
        rc, out, _ = self.cli("list", "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn(f"0 pending questions in the owner's DM room {ROOM}", out)


class TestResolve(_Ws):
    def test_resolve_closes_the_row_and_never_reopens_it(self):
        store = self.store()
        a = self.ask("first?", store)
        rc, out, _ = self.cli("resolve", a["ask_id"], "--answered", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{a['ask_id']} -> Answered", out)
        self.assertEqual(self.store().status_of(a["ask_id"]), "Answered")
        rc, out, _ = self.cli("resolve", a["ask_id"], "--workspace", str(self.ws))
        self.assertEqual(rc, 1)
        self.assertIn("not changed", out)
        self.assertEqual(self.store().status_of(a["ask_id"]), "Answered")

    def test_an_unknown_id_is_refused_with_and_without_the_capability_and_a_known_one_closes_locally(self):
        rc, out, _ = self.cli("resolve", "ask-nope", "--workspace", str(self.ws))
        self.assertEqual(rc, 1)
        self.assertIn("not changed", out)
        a = self.ask("first?", self.store())  # a confirmed row: the store has history
        shutil.rmtree(self.ws / "skills")
        rc, out, _ = self.cli("resolve", "ask-nope", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        self.assertIn("recorded locally as Resolved", out)
        self.assertIn("outage mode", out)
        rc, out, _ = self.cli("resolve", a["ask_id"], "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        fresh = self.ws / "fresh"
        (fresh / "state").mkdir(parents=True)
        rc, out, _ = self.cli("resolve", "ask-nope", "--workspace", str(fresh))
        self.assertEqual(rc, 1, out)
        self.assertIn("no held question ask-nope and no room row was ever confirmed here", out)
        self.assertEqual(adapter.count(fresh), {"open": 0, "done": 0, "pending_close": 0, "unavailable": False, "reason": None})

    def test_a_close_during_an_outage_is_applied_by_the_next_reconcile(self):
        a = self.ask("first?", self.store())
        with mock.patch.dict(os.environ, {"FAKE_ROOM_FAIL": "1"}):
            rc, out, _ = self.cli("resolve", a["ask_id"], "--answered", "--workspace", str(self.ws))
        self.assertEqual(rc, 0, out)
        self.assertIn("recorded locally as Answered", out)
        self.assertEqual(self.store().status_of(a["ask_id"]), "Open")
        rc, out, _ = self.cli("reconcile", "--workspace", str(self.ws))
        self.assertIn("applied 1 local close", out)
        self.assertEqual(self.store().status_of(a["ask_id"]), "Answered")
        self.assertEqual(sorted((self.ws / "state" / "pending-questions-outbox" / "closed").glob("*.json")), [])


class TestRemind(unittest.TestCase):
    def _argv(self, *args):
        with mock.patch.object(pq.subprocess, "run") as run:
            run.return_value.returncode = 3
            self.assertEqual(pq.main(["remind", *args]), 3)
        return run.call_args[0][0]

    def test_remind_passes_notify_and_the_adapter_through(self):
        argv = self._argv("--force")
        self.assertEqual(argv, [sys.executable, str(REPO / "src" / "check-pending-questions.py"),
                                "--notify", "--force", "--store-adapter", str(pq.ADAPTER)])
        self.assertEqual(self._argv("--store-adapter", "x.py")[2:], ["--notify", "--store-adapter", "x.py"])

    def test_an_unknown_verb_prints_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pq.main(["nope"]), 2)
        self.assertIn("pq.py ask", err.getvalue())


class TestDeclaration(rdb._Ws):
    def _skill(self, name, manifest, script="scripts/a.py"):
        d = self.ws / "skills-dir" / name
        (d / "scripts").mkdir(parents=True)
        (d / script).write_text("def room_store(ws):\n    return None, 'fake'\n")
        (d / "manifest.json").write_text(json.dumps(manifest))
        return d

    def test_the_repo_skill_declares_its_adapter(self):
        self.assertEqual(skill_roots.declared_script(reader.DECLARATION, REPO / "skills"), pq.ADAPTER)
        self.assertEqual(Path(reader._adapter(skill_roots.declared(reader.DECLARATION, self.ws))[1]), pq.ADAPTER)

    def test_a_declaration_must_stay_inside_its_skill(self):
        skills = self.ws / "skills-dir"
        self._skill("a-off", {"enabled": False, "pending_questions_store": "scripts/a.py"})
        self._skill("b-out", {"pending_questions_store": "../a-off/scripts/a.py"})
        self.assertIsNone(skill_roots.declared_script(reader.DECLARATION, skills))
        good = self._skill("c-ok", {"pending_questions_store": "scripts/a.py"})
        self.assertEqual(skill_roots.declared_script(reader.DECLARATION, skills), (good / "scripts" / "a.py").resolve())
        self.assertEqual(reader.load_adapter(skill_roots.declared_script(reader.DECLARATION, skills)).room_store(self.ws), (None, "fake"))
        self.assertIsNone(reader.load_adapter(None))

    def test_two_skills_declaring_the_store_is_a_refusal_not_an_alphabetical_pick(self):
        skills = self.ws / "skills-dir"
        self._skill("aaa-first", {"pending_questions_store": "scripts/a.py"})
        self._skill("zzz-last", {"pending_questions_store": "scripts/a.py"})
        with self.assertRaisesRegex(skill_roots.DeclarationConflict, "aaa-first, zzz-last"):
            skill_roots.declared_script(reader.DECLARATION, skills)
        g = reader.gather(self.ws, skill_roots.declared(reader.DECLARATION, roots=skills))
        self.assertTrue(g["unavailable"])
        self.assertIn("more than one skill declares", g["reason"])
        self.assertEqual(reader.count(self.ws, skill_roots.declared(reader.DECLARATION, roots=skills))["open"], None)

    def test_without_the_skill_the_reader_is_unavailable_and_closes_nothing(self):
        out = pqa.ask_owner("held?", urgency="durable", workspace=self.ws, host=HOST)
        g = reader.gather(self.ws, skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))
        self.assertEqual((g["unavailable"], g["done"], g["waiting"]), (True, None, []))
        self.assertIn("no skill declares one", g["reason"])
        ok, msg = reader.resolve(self.ws, out["ask_id"], "Resolved", skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))
        self.assertFalse(ok)
        self.assertIn("nothing records the close", msg)
        self.assertEqual(reader.count(self.ws, skill_roots.declared(reader.DECLARATION, roots=self.ws / "no-skills"))["open"], None)
        g = reader.gather(self.ws, skill_roots.declared(reader.DECLARATION, roots=REPO / "skills"))  # the skill, without its room
        self.assertEqual(([i["ask_id"] for i in g["waiting"]], g["unavailable"], g["done"]), ([out["ask_id"]], False, 0))

    def test_without_the_skill_ask_owner_keeps_a_generic_record_only(self):
        cli = REPO / "scripts" / "ask-owner.py"
        with mock.patch.object(skill_roots, "declared_script", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = _load("ask_owner_cli", cli).main(["q?", "--urgency", "durable", "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        self.assertIn("recorded: NO STORE — no pending-questions store adapter installed", out.getvalue())
        self.assertEqual(self.outbox(), [], "the outbox is the skill's")
        self.assertEqual(len(list((self.ws / "state" / "ask-owner").glob("ask-*.json"))), 1)


class TestSharedRoom(rdb._Ws):
    SHARED = "!shared:test.invalid"

    def store_room(self, env):
        rdb._install_fake_capability(self.ws)
        return adapter.room_store(self.ws, environ=env)

    def test_the_owner_dm_is_the_default(self):
        store, room = self.store_room({})
        self.assertEqual(room, ROOM)
        self.assertIn("DM room", store.label)

    def test_a_host_state_file_moves_the_database_to_the_shared_room(self):
        (self.ws / "state" / "pending-questions-room").write_text(self.SHARED + "\n")
        store, room = self.store_room({})
        self.assertEqual(room, self.SHARED)
        self.assertNotIn("DM", store.label)
        self.assertIn(self.SHARED, store.link)

    def test_env_wins_over_the_state_file(self):
        (self.ws / "state" / "pending-questions-room").write_text("!fromstate:test.invalid")
        self.assertEqual(self.store_room({"PENDING_QUESTIONS_ROOM": self.SHARED})[1], self.SHARED)

    def test_the_canonical_capability_skill_is_looked_up_before_its_alias(self):
        rdb._install_fake_capability(self.ws)
        canonical = self.ws / "skills" / "room-commons" / "scripts"
        shutil.copytree(self.ws / "skills" / "room-collab" / "scripts", canonical)
        self.assertEqual(adapter.skill_scripts(self.ws), canonical)
        shutil.rmtree(canonical.parent)
        self.assertEqual(adapter.skill_scripts(self.ws), self.ws / "skills" / "room-collab" / "scripts")

    def test_the_manifest_declares_the_key_empty(self):
        cfg = json.loads((SKILL / "manifest.json").read_text())
        self.assertEqual(cfg["config"], {"PENDING_QUESTIONS_ROOM": "", "PENDING_QUESTIONS_COLLAB_URL": ""})

    def test_no_schedule_mentions_the_reminder(self):
        crons = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
        self.assertEqual([c for c in crons if "pending-questions" in json.dumps(c)], [])

    def test_no_pass_surfaces_questions(self):
        """The proactive loop and the briefing hand the owner no question on a schedule."""
        for rel in ("skills/proactive-loop/SKILL.md", "docs/proactive-loop-rationale.md"):
            text = (REPO / rel).read_text()
            self.assertNotRegex(text, r"question-\{?<?ts>?\}?\.txt", rel)
            self.assertNotRegex(text, r"pq\.py list`?[^\n]*surface", rel)
        self.assertNotIn("pending_qs[0]", (REPO / "src" / "morning-briefing.py").read_text())


if __name__ == "__main__":
    unittest.main()
