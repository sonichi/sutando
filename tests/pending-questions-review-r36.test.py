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
skill_roots = importlib.import_module("skill_roots")
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


# ---- 1. a direct resolve commits the history before the close ------------------------------

class ResolveCommitsHistoryFirst(_FakeRoom):
    def pre_marker_row(self, ask_id="ask-existing"):
        """A legitimate complete row of this workspace with no local evidence of it."""
        self.store().insert(self.q(ask_id, "from before?"), SENT)
        for marker in (pqo.STORE_HISTORY, pqo.ROOM_INTRODUCED):
            (self.ws / "state" / marker).unlink(missing_ok=True)
        return ask_id

    def test_a_marker_that_cannot_be_written_leaves_the_row_open_and_records_the_close_locally(self):
        """The reviewer's injection: a complete pre-marker row, mark_store_used raising
        OSError('disk full') through the production adapter, then the capability removed."""
        aid = self.pre_marker_row()
        with _marker_fails(adapter, pqs, pqo):
            ok, msg = adapter.resolve(self.ws, aid, "Answered")
        self.assertTrue(ok, msg)
        self.assertIn("recorded locally as Answered", msg)
        self.assertIn("store history not committed (OSError: disk full)", msg)
        self.assertIn("its row is in view", msg)
        self.assertEqual([(e["ask_id"], e["status"]) for e in self.store().entries()], [(aid, "Open")],
                         "the row was closed with no local evidence of it")
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.closes(), [aid], "the close record is the evidence")
        self.assertEqual(self.gathered(), {"waiting": [], "done": 1, "pending_close": [], "unavailable": False},
                         "with the room in view the local close counts the row as done")
        self.lose_capability()
        g = self.gathered()
        self.assertTrue(g["unavailable"], f"capability loss read as a measured zero: {g}")
        self.assertEqual((g["done"], g["pending_close"]), (None, [aid]))
        self.assertNotEqual((g["waiting"], g["done"]), ([], 0))

    def test_the_next_reconcile_applies_the_local_close_and_commits_the_history(self):
        aid = self.pre_marker_row()
        with _marker_fails(adapter, pqs, pqo):
            adapter.resolve(self.ws, aid, "Answered")
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual((rec["closed"], rec["errors"]), ([aid], []))
        self.assertEqual(self.store().status_of(aid), "Answered")
        self.assertEqual(self.marker().read_text().strip(), aid)
        self.assertEqual(self.closes(), [])
        self.lose_capability()
        self.assertTrue(self.gathered()["unavailable"])

    def test_a_pre_marker_row_closes_in_the_room_once_the_marker_is_committed(self):
        """The marker lands before the close: the row is closed AND the workspace has its history."""
        aid = self.pre_marker_row()
        ok, msg = adapter.resolve(self.ws, aid, "Resolved")
        self.assertTrue(ok, msg)
        self.assertIn(f"room database: {aid} -> Resolved", msg)
        self.assertEqual(self.store().status_of(aid), "Resolved")
        self.assertEqual(self.marker().read_text().strip(), aid)
        self.assertEqual(self.closes(), [])
        self.lose_capability()
        self.assertTrue(self.gathered()["unavailable"])

    def test_an_unknown_id_on_a_pre_marker_store_writes_no_marker_and_no_record(self):
        """No row in view: nothing is committed for it, the close is refused, no count changes."""
        self.pre_marker_row("ask-other")
        with _marker_fails(adapter, pqs, pqo):
            ok, msg = adapter.resolve(self.ws, "ask-never-existed", "Answered")
        self.assertFalse(ok, msg)
        self.assertIn("not changed — GuardFailed: ask-never-existed: left as is (no such row)", msg)
        self.assertEqual(self.closes(), [])
        self.assertFalse(self.marker().exists())

    def test_a_marker_already_committed_costs_the_close_no_extra_read(self):
        store = self.store()
        self.ask("q?", store=store)
        [e] = store.entries()
        self.assertTrue(self.marker().exists())
        with mock.patch.object(pqs.RoomDbStore, "status_of", side_effect=AssertionError("read the row again")):
            ok, msg = adapter.resolve(self.ws, e["ask_id"], "Answered")
        self.assertTrue(ok, msg)
        self.assertEqual(self.store().status_of(e["ask_id"]), "Answered")

    def test_a_close_record_naming_no_held_question_is_history(self):
        """The evidence rule: such a record is only written with the row in view or in outage mode."""
        self.assertFalse(pqo.store_history(self.ws))
        held = self.ask("held?")
        pqs.Outbox(self.ws).close(held["ask_id"], "Resolved")
        self.assertFalse(pqo.store_history(self.ws), "a close of a held question is a measured closure")
        pqs.Outbox(self.ws).close("ask-row-elsewhere", "Answered")
        self.assertTrue(pqo.store_history(self.ws))
        self.assertEqual(adapter.local_close_allowed(self.ws, "ask-another-row")[1],
                         "outage mode: a room row was confirmed here before")


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


# ---- 3. `pq.py remind` reconciles before it reminds --------------------------------------

class RemindReconciles(_FakeRoom):
    def test_the_documented_command_files_the_held_ask_before_reminding(self):
        """The real `pq.py remind` subprocess (check-pending-questions.py with the sibling adapter
        injected) under the fake capability, with one held ask. The ask's queued DM is drained, so
        nothing is due and no notification fires; the pass must still have filed the row."""
        held = self.ask("held?")
        aid = held["ask_id"]
        (self.ws / "results" / held["proactive_file"]).unlink()
        self.assertEqual(self.outbox(), [f"{aid}.json"])
        env = {**ENV, "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_HOST_LABEL": HOST,
               "FAKE_ROOM_STATE": str(self.ws / "fake-room.json")}
        r = subprocess.run([sys.executable, str(SKILL / "scripts" / "pq.py"), "remind"], capture_output=True,
                           text=True, timeout=300, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.outbox(), [], f"the held ask was not filed by the reminder's pass:\n{r.stdout}{r.stderr}")
        [row] = self.store().entries()
        self.assertEqual((row["ask_id"], row["status"], row["incomplete"]), (aid, "Open", False))
        self.assertTrue((self.ws / "state" / pqo.STORE_HISTORY).exists())
        self.assertNotIn("not yet in the room", r.stdout)
        self.assertIn("(sent) 1 pending questions", r.stdout, "the filed row was read, and found just asked")
        self.assertEqual([p.name for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-pending-q-")], [])

    def test_the_injected_branch_reads_through_the_pass(self):
        cpq = rdb._cpq(self.ws)
        held = self.ask("held?")
        with mock.patch.object(sys, "argv", ["check-pending-questions.py", "--store-adapter", str(ADAPTER)]), \
                rdb.contextlib.redirect_stdout(rdb.io.StringIO()) as out, rdb.contextlib.redirect_stderr(rdb.io.StringIO()):
            self.assertEqual(cpq.main(), 0)
        self.assertEqual(self.outbox(), [])
        self.assertEqual([e["ask_id"] for e in self.store().entries()], [held["ask_id"]])
        self.assertIn(f"- [{held['ask_id']}] held?\n", out.getvalue())


# ---- 4. the linter resolves the adapter path as discovery does ---------------------------

class LinterMatchesDiscovery(unittest.TestCase):
    linter = _load("lint_skill_r36", REPO / "scripts" / "lint-skill.py")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lint-r36-"))
        self.skills = self.tmp / "skills"
        self.skill = self.skills / "pq"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "manifest.json").write_text(json.dumps(
            {"name": "pq", "version": "1.0.0", "owner": "x", "stability": "experimental",
             "pending_questions_store": "scripts/adapter.py"}))

    def lint(self):
        errors, _ = self.linter._lint_manifest(self.skill)
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "lint-skill.py"), str(self.skill)],
                           capture_output=True, text=True, timeout=60)
        return [e for e in errors if "pending_questions_store" in e], r.returncode, r.stdout

    def test_a_symlink_escaping_the_skill_is_a_lint_error_as_it_is_a_discovery_miss(self):
        """The reviewer's injection: scripts/adapter.py is a symlink to a file outside the skill."""
        outside = self.tmp / "elsewhere" / "adapter.py"
        outside.parent.mkdir()
        outside.write_text("def gather(ws):\n    return {}\n")
        os.symlink(outside, self.skill / "scripts" / "adapter.py")
        self.assertTrue((self.skill / "scripts" / "adapter.py").is_file(), "the textual check alone passes it")
        errors, rc, out = self.lint()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("must resolve inside the skill directory", errors[0])
        self.assertIn(str(outside.resolve()), errors[0])
        self.assertEqual(rc, 1, out)
        self.assertIn("1 error(s)", out)
        self.assertEqual(skill_roots.declared_scripts(reader.DECLARATION, self.skills), [], "runtime discovery rejects it too")

    def test_a_symlink_inside_the_skill_passes_both(self):
        impl = self.skill / "impl" / "adapter.py"
        impl.parent.mkdir()
        impl.write_text("def gather(ws):\n    return {}\n")
        os.symlink(Path("..") / "impl" / "adapter.py", self.skill / "scripts" / "adapter.py")
        errors, rc, _ = self.lint()
        self.assertEqual((errors, rc), ([], 0))
        self.assertEqual([(n, p) for n, p in skill_roots.declared_scripts(reader.DECLARATION, self.skills)], [("pq", impl.resolve())])

    def test_a_plain_file_passes_and_the_real_manifests_stay_clean(self):
        (self.skill / "scripts" / "adapter.py").write_text("def gather(ws):\n    return {}\n")
        self.assertEqual(self.lint()[:2], ([], 0))
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "lint-skill.py"), "--all"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 error(s)", r.stdout)


# ---- the doc note ------------------------------------------------------------------------

class DocDistinguishesTheCooldown(unittest.TestCase):
    def test_no_scheduled_reminder_is_told_apart_from_the_on_demand_cooldown(self):
        text = (REPO / "docs" / "design-mediated-capability-layer.md").read_text()
        self.assertIn("there is no scheduled reminder at all", text)
        self.assertIn("on-demand reminder still has a cooldown", text)
        self.assertIn("unless `--force` is passed", text)
        self.assertNotIn("neither the divider nor the cooldown exists on the current path", text)
        catalog = json.loads((REPO / "docs" / "catalog.json").read_text())["documents"]
        self.assertEqual(catalog["docs/design-mediated-capability-layer.md"]["last_verified"], "2026-10-02")


if __name__ == "__main__":
    unittest.main()
