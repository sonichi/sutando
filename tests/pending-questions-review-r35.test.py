#!/usr/bin/env python3
"""The failure states of review round 35 on #5027, each reproduced by the reviewer's own fault
injection against the production code and pinned to the fixed transition: (1) the store-history
marker is committed BEFORE any local evidence is released — the direct ask's outbox entry, a
flushed entry, a replayed close, a moved legacy entry — and a marker that cannot be written keeps
that evidence, so losing the capability afterwards never reads as zero; (2) two /answer calls in
the same millisecond are two task files, both bodies kept, never one overwritten under two 200s;
(3) a local close is not consumed until its row is complete, and gather() lists an ask id in one
bucket only; (4) the Questions UI shows the outage beside the rows it holds locally instead of
"N of N"; (5) gather() is read-only — a workspace without the marker stays without it, and a
marker write that would fail never makes a reachable room read as unavailable; (6) the manifest
field is in the canonical schema, the linter validates it, the collab URL is declared config.
No real room: the fake capability and in-process store of the room-db suite."""
import datetime as _dt
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
compat = importlib.import_module("pending_questions_compat")
local_record = importlib.import_module("local_record")
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
                "unavailable": g["unavailable"]}


# ---- 1. the history marker is committed before any local evidence goes --------------------

class HistoryBeforeEvidence(_FakeRoom):
    def test_a_direct_ask_whose_marker_write_fails_keeps_its_outbox_entry(self):
        """The reviewer's injection: a real row lands, the introduction cannot be queued, and the
        marker writer raises OSError('disk full'); then the capability is removed."""
        (self.ws / "results").rmdir()
        (self.ws / "results").write_text("not a directory")
        store = self.store()
        with _marker_fails(adapter, pqs, pqo):
            out = self.ask("q?", store=store)
        self.assertEqual(out["history_error"], "OSError: disk full")
        self.assertEqual(len(store.entries()), 1, "the row landed")
        self.assertIsNotNone(out["outbox"], "the entry is the local evidence; it stays")
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])
        self.assertFalse(self.marker().exists())
        self.assertIn("its outbox entry is kept until a reconcile commits the history",
                      "\n".join(adapter.report_lines(out)))
        self.lose_capability()
        g = self.gathered()
        self.assertEqual(g["waiting"], [(out["ask_id"], False)], f"capability loss read as nothing: {g}")
        self.assertNotEqual((g["waiting"], g["done"]), ([], 0))

    def test_the_next_reconcile_commits_the_marker_then_releases_the_entry(self):
        store = self.store()
        with _marker_fails(adapter, pqs, pqo):
            out = self.ask("q?", store=store)
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual((rec["flushed"], rec["errors"]), ([out["ask_id"]], []))
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(self.store().entries()), 1, "the replay resumed the existing row, no second one")

    def test_a_flush_whose_marker_write_fails_keeps_the_entry_and_says_so(self):
        """The reviewer's second ordering: reconcile lands the row, then the marker fails."""
        held = self.ask("held?")
        with _marker_fails(adapter, pqs, pqo):
            rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(rec["flushed"], [])
        self.assertTrue(any(a.startswith(f"{held['ask_id']}: StoreError: store history not committed")
                            for a in rec["errors"]), rec)
        self.assertEqual(len(self.store().entries()), 1, "the row is there")
        self.assertEqual(self.outbox(), [f"{held['ask_id']}.json"], "and so is the evidence")
        self.assertFalse(self.marker().exists())
        self.lose_capability()
        self.assertEqual(self.gathered()["waiting"], [(held["ask_id"], False)])

    def test_a_replayed_close_whose_marker_write_fails_keeps_the_close_record(self):
        store = self.store()
        self.ask("q?", store=store)
        [e] = store.entries()
        self.marker().unlink()
        pqs.Outbox(self.ws).close(e["ask_id"], "Resolved")
        ob = pqs.Outbox(self.ws)
        with _marker_fails(adapter, pqs, pqo):
            closed, errors = ob.replay_closes(store)
        self.assertEqual(closed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("store history not committed", errors[0])
        self.assertEqual(sorted(ob.closes()), [e["ask_id"]])
        self.assertEqual(store.status_of(e["ask_id"]), "Resolved", "the row is closed; only the record waits")
        closed, errors = ob.replay_closes(store)
        self.assertEqual((closed, errors), ([e["ask_id"]], []))
        self.assertTrue(self.marker().exists())

    def test_a_legacy_entry_whose_marker_write_fails_is_not_marked_moved(self):
        legacy = self.ws / "hosts" / HOST / "pending-questions.md"
        legacy.write_text("# Open\n\n## Prose question?\n\nbody\n\n")
        with _marker_fails(adapter, pqs, pqo, compat):
            rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(rec["moved"], [])
        self.assertTrue(any("disk full" in e for e in rec["errors"]), rec)
        self.assertNotIn("moved", legacy.read_text())
        self.assertEqual(len(self.store().entries()), 1)
        rec = adapter.reconcile_pass(self.ws, environ={})
        self.assertEqual(len(rec["moved"]), 1, rec)
        self.assertIn("**Status:** moved", legacy.read_text())
        self.assertTrue(self.marker().exists())


# ---- 2. two answers in one millisecond are two files -----------------------------------

FROZEN = _dt.datetime(2026, 10, 2, 12, 0, 0, 123000, tzinfo=_dt.timezone.utc)


class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz else FROZEN.replace(tzinfo=None)


class AnswerRace(unittest.TestCase):
    def setUp(self):
        self.api = _load("agent_api_r35", REPO / "src" / "agent-api.py")
        self.tmp = Path(tempfile.mkdtemp(prefix="r35-answer-"))
        (self.tmp / "tasks").mkdir()
        self.api.WORKSPACE_DIR, self.api.TASK_DIR = self.tmp, self.tmp / "tasks"
        item = {"id": "ask-race", "ask_id": "ask-race", "title": "ask-race", "snippet": "", "body": "",
                "asked_at": None, "priority": None, "in_room": True}
        self.g = {"waiting": [item], "done": 0, "pending_close": [], "unavailable": False, "reason": None,
                  "link": None, "notes": [], "store": None}

    def files(self):
        return sorted((self.tmp / "tasks").glob("answer-*.txt"))

    def test_two_answers_in_the_same_millisecond_synchronized_at_resolve_are_both_kept(self):
        """The reviewer's injection: the clock frozen on one millisecond, both requests held at
        resolve() until the other has filed its task."""
        barrier = threading.Barrier(2)

        def _resolve(ws, a, s, store=None):
            barrier.wait(timeout=10)
            return True, "closed"
        responses = {}
        with mock.patch.object(self.api.pending_questions_reader, "gather", return_value=self.g), \
                mock.patch.object(self.api.pending_questions_reader, "resolve", _resolve), \
                mock.patch.object(local_record, "datetime", _Frozen):
            ts = [threading.Thread(target=lambda k, ans: responses.__setitem__(k, self.api.answer_question("ask-race", ans)),
                                   args=(k, ans)) for k, ans in (("a", "first owner answer"), ("b", "second owner answer"))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual({k: v[0] for k, v in responses.items()}, {"a": 200, "b": 200}, responses)
        names = {Path(v[1]["task"]).name for v in responses.values()}
        self.assertEqual(len(names), 2, f"two 200s pointed at one file: {names}")
        ms = f"answer-ask-race-{int(FROZEN.timestamp() * 1000)}-"
        self.assertTrue(all(n.startswith(ms) for n in names), (ms, names))
        bodies = sorted(p.read_text() for p in self.files())
        self.assertEqual(bodies, ["User answered ask-race: first owner answer",
                                  "User answered ask-race: second owner answer"])

    def test_a_name_already_taken_is_never_replaced(self):
        """Every candidate name collides: the earlier answer's file is untouched and the second
        answer is refused rather than filed over it."""
        taken = self.tmp / "tasks" / "answer-ask-race-1-2-abc.txt"
        taken.write_text("User answered ask-race: first owner answer")
        with mock.patch.object(self.api.local_record, "new_name", return_value="answer-ask-race-1-2-abc"):
            path, err = self.api._file_answer_task("ask-race", "second owner answer")
        self.assertIsNone(path)
        self.assertEqual(err, "no free task name after 8 tries")
        self.assertEqual(taken.read_text(), "User answered ask-race: first owner answer")
        self.assertEqual(self.files(), [taken])
        self.assertEqual(list((self.tmp / "tasks").glob(".*")), [], "no temp left beside it")

    def test_an_id_too_long_for_a_record_name_is_refused_not_raised(self):
        path, err = self.api._file_answer_task("x" * 250, "answer")
        self.assertIsNone(path)
        self.assertIn("record name", err)
        self.assertEqual(self.files(), [])

    def test_exclusive_creation_fails_closed_on_an_existing_file(self):
        p = self.tmp / "tasks" / "x.txt"
        local_record.create_text_whole(p, "one")
        with self.assertRaises(FileExistsError):
            local_record.create_text_whole(p, "two")
        self.assertEqual(p.read_text(), "one")
        self.assertEqual(list((self.tmp / "tasks").glob(".*")), [])


# ---- 3. a close waits for a complete row; one ask id, one bucket -------------------------

class _NoBody(rdb.fake_client.FakeDoc):
    async def put_row_body(self, db, row, text, append=False):
        raise ConnectionError("socket lost before body")


class CloseWaitsForTheBody(rdb._Ws):
    def test_a_close_is_not_consumed_while_the_body_write_keeps_failing(self):
        """The reviewer's injection: put_row_body faults on every attempt."""
        held = self.ask("held?")
        aid = held["ask_id"]
        pqs.Outbox(self.ws).close(aid, "Answered")
        doc = _NoBody()
        store = self.db(rdb.InProcClient(doc))
        with mock.patch.object(adapter, "room_store", return_value=(store, "the room")):
            rec = adapter.reconcile_pass(self.ws, environ={})
            self.assertEqual((rec["flushed"], rec["closed"]), ([], []))
            self.assertEqual(rec["errors"], [f"{aid}: ConnectionError: socket lost before body",
                                             f"close {aid}: StoreError: its row is still incomplete (no body yet); kept until it lands"])
            [row] = store.entries()
            self.assertEqual((row["status"], row["incomplete"], row["body"]), ("Answered", True, ""))
            self.assertEqual(self.outbox(), [f"{aid}.json", "closed"], "the entry and the close are both held")
            self.assertEqual(sorted(pqs.Outbox(self.ws).closes()), [aid])
            g = adapter.gather(self.ws, environ={})
            self.assertEqual(([it["ask_id"] for it in g["waiting"]], g["done"], g["pending_close"]), ([], 1, []),
                             "the same ask was reported both waiting and done")
            # the body lands on a later pass: the entry is flushed, the close retired, still one bucket
            good = self.db(rdb.InProcClient(rdb.fake_client.FakeDoc(doc.state())))
        with mock.patch.object(adapter, "room_store", return_value=(good, "the room")):
            rec = adapter.reconcile_pass(self.ws, environ={})
            self.assertEqual((rec["flushed"], rec["closed"], rec["errors"]), ([aid], [aid], []))
            [row] = good.entries()
            self.assertEqual((row["status"], row["incomplete"]), ("Answered", False))
            self.assertIn("held?", row["body"])
            self.assertEqual(self.outbox(), ["closed"])
            self.assertEqual(pqs.Outbox(self.ws).closes(), {})
            g = adapter.gather(self.ws, environ={})
            self.assertEqual(([it["ask_id"] for it in g["waiting"]], g["done"], g["pending_close"]), ([], 1, []))

    def test_every_ask_id_lands_in_exactly_one_bucket(self):
        """The precedence: terminal/closed row → done; complete open row → waiting (in room);
        incomplete row → its held entry waits (not in room); held + closed locally → done;
        a close naming neither → pending_close."""
        doc = rdb.fake_client.FakeDoc()
        store = self.db(rdb.InProcClient(doc))
        ob = pqs.Outbox(self.ws)
        store.insert(self.q("ask-done", "done?"), SENT)
        store.close("ask-done", "Resolved")
        ob.save(self.q("ask-done", "done?"), SENT)                    # a stale held entry of a closed row
        store.insert(self.q("ask-open", "open?"), SENT)
        ob.save(self.q("ask-open", "open?"), SENT)                    # a held entry of a complete open row
        nobody = self.db(rdb.InProcClient(_NoBody(doc.state())))
        with self.assertRaises(ConnectionError):
            nobody.insert(self.q("ask-nobody", "no body?"), SENT)     # an incomplete row
        doc = rdb.fake_client.FakeDoc(nobody.client.doc.state())
        store = self.db(rdb.InProcClient(doc))
        ob.save(self.q("ask-nobody", "no body?"), SENT)
        ob.save(self.q("ask-held", "held?"), SENT)                    # held, no row
        ob.save(self.q("ask-held-closed", "held closed?"), SENT)
        ob.close("ask-held-closed", "Answered")                       # held, closed locally, no row
        ob.close("ask-elsewhere", "Answered")                         # a close naming nothing here
        with mock.patch.object(adapter, "room_store", return_value=(store, "the room")):
            g = adapter.gather(self.ws, environ={})
        waiting = [(it["ask_id"], it["in_room"]) for it in g["waiting"]]
        self.assertEqual(waiting, [("ask-open", True), ("ask-held", False), ("ask-nobody", False)],
                         "rows first, then the held entries by name")
        self.assertEqual(g["done"], 2, "ask-done (row) and ask-held-closed (held, closed locally)")
        self.assertEqual(g["pending_close"], ["ask-elsewhere"])
        ids = [a for a, _ in waiting] + ["ask-done", "ask-held-closed"] + g["pending_close"]
        self.assertEqual(len(ids), len(set(ids)), "an ask id is in two buckets")
        self.assertFalse(self.ws.joinpath("state", pqo.STORE_HISTORY).exists(), "a read wrote the marker")


# ---- 5. gather() is read-only ------------------------------------------------------------

class GatherIsReadOnly(_FakeRoom):
    def _state_files(self):
        return sorted(str(p.relative_to(self.ws)) for p in (self.ws / "state").rglob("*"))

    def test_a_pre_existing_row_with_no_marker_is_listed_and_no_file_appears(self):
        """The reviewer's injection: rows exist in the room; this workspace has no marker."""
        self.store().insert(self.q("ask-old", "from before?"), SENT)
        for marker in (pqo.STORE_HISTORY, pqo.ROOM_INTRODUCED):
            (self.ws / "state" / marker).unlink(missing_ok=True)
        before = self._state_files()
        self.assertFalse(self.marker().exists())
        g = adapter.gather(self.ws, environ={})
        self.assertEqual(([it["ask_id"] for it in g["waiting"]], g["unavailable"]), (["ask-old"], False))
        self.assertFalse(self.marker().exists(), "the read wrote the history marker")
        self.assertEqual(self._state_files(), before, "a read wrote a file")
        self.assertEqual(reader.count(self.ws, adapter=ADAPTER)["open"], 1)
        self.assertEqual(self._state_files(), before)

    def test_a_marker_write_that_would_fail_never_makes_a_reachable_room_unavailable(self):
        self.store().insert(self.q("ask-old", "from before?"), SENT)
        self.marker().unlink(missing_ok=True)
        with _marker_fails(adapter, pqs, pqo):
            g = adapter.gather(self.ws, environ={})
        self.assertEqual((g["unavailable"], g["reason"], [it["ask_id"] for it in g["waiting"]]), (False, None, ["ask-old"]))

    def test_the_explicit_pass_is_where_a_pre_existing_row_becomes_history(self):
        self.store().insert(self.q("ask-old", "from before?"), SENT)
        self.marker().unlink(missing_ok=True)
        self.assertEqual(adapter.reconcile_pass(self.ws, environ={})["errors"], [])
        self.assertEqual(self.marker().read_text().strip(), "ask-old")
        self.lose_capability()
        self.assertTrue(adapter.gather(self.ws, environ={})["unavailable"])


# ---- 6. contracts: the schema, the linter, the declared config ---------------------------

def _validate(manifest: dict, schema: dict) -> list:
    """The checks the schema states for a top-level manifest, stdlib only: closed property set,
    required fields, string types, patterns and enums. (jsonschema is not a dependency here.)"""
    import re
    errors = []
    props = schema["properties"]
    if schema.get("additionalProperties") is False:
        errors += [f"additional property {k!r}" for k in manifest if k not in props]
    errors += [f"missing {k!r}" for k in schema.get("required", []) if k not in manifest]
    for k, v in manifest.items():
        spec = props.get(k)
        if not spec:
            continue
        if spec.get("type") == "string" and not isinstance(v, str):
            errors.append(f"{k}: not a string")
        elif spec.get("type") == "string":
            if "pattern" in spec and not re.match(spec["pattern"], v):
                errors.append(f"{k}: {v!r} does not match {spec['pattern']}")
            if "enum" in spec and v not in spec["enum"]:
                errors.append(f"{k}: {v!r} not in {spec['enum']}")
        elif spec.get("type") == "object" and not isinstance(v, dict):
            errors.append(f"{k}: not an object")
    return errors


class ManifestContract(unittest.TestCase):
    schema = json.loads((REPO / "schemas" / "skill-manifest.schema.json").read_text())
    manifest = json.loads((SKILL / "manifest.json").read_text())
    lint = _load("lint_skill_r35", REPO / "scripts" / "lint-skill.py")

    def test_the_canonical_manifest_validates_against_the_canonical_schema(self):
        self.assertTrue("pending_questions_store" in self.schema["properties"], "the field is not in the schema")
        self.assertEqual([k for k in self.manifest if k not in self.schema["properties"]], [],
                         "manifest keys absent from the schema")
        self.assertEqual(_validate(self.manifest, self.schema), [])
        try:
            import jsonschema  # noqa: PLC0415
        except ImportError:
            return
        jsonschema.validate(self.manifest, self.schema)

    def test_the_validator_rejects_what_the_schema_forbids(self):
        self.assertEqual(_validate({**self.manifest, "bogus": 1}, self.schema), ["additional property 'bogus'"])
        for bad in ("../x.py", "/abs/x.py", "scripts/../x.py", "scripts/x.sh", "scripts/a b.py", ""):
            self.assertTrue(_validate({**self.manifest, "pending_questions_store": bad}, self.schema), bad)
        self.assertTrue(_validate({**self.manifest, "pending_questions_store": 3}, self.schema))

    def test_the_linter_validates_the_field_rather_than_only_allowing_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="lint-pqs-"))

        def lint(value):
            d = tmp / "pq"
            shutil.rmtree(d, ignore_errors=True)
            (d / "scripts").mkdir(parents=True)
            (d / "scripts" / "adapter.py").write_text("# adapter\n")
            (d / "manifest.json").write_text(json.dumps({"name": "pq", "version": "1.0.0", "owner": "x",
                                                         "stability": "experimental", "pending_questions_store": value}))
            errors, warnings = self.lint._lint_manifest(d)
            return [e for e in errors if "pending_questions_store" in e], [w for w in warnings if "pending_questions_store" in w]
        self.assertEqual(lint("scripts/adapter.py"), ([], []))
        self.assertIn("must stay inside the skill directory", lint("../adapter.py")[0][0])
        self.assertIn("must stay inside the skill directory", lint("/etc/adapter.py")[0][0])
        self.assertIn("does not exist", lint("scripts/missing.py")[0][0])
        self.assertIn("relative path to a .py script", lint("scripts/adapter.sh")[0][0])
        self.assertIn("relative path to a .py script", lint(7)[0][0])
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "lint-skill.py"), str(SKILL)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 error(s), 0 warning(s)", r.stdout)

    def test_the_collab_url_is_declared_config_read_by_the_documented_precedence(self):
        self.assertIn(adapter.URL_KEY, self.manifest["config"], "declared in the manifest config block")
        self.assertEqual(adapter.configured(adapter.URL_KEY, {}), "", "unset by default")
        with mock.patch.object(adapter, "manifest_config", return_value="https://from-manifest.test.invalid"):
            self.assertEqual(adapter.configured(adapter.URL_KEY, {}), "https://from-manifest.test.invalid")
            self.assertEqual(adapter.configured(adapter.URL_KEY, {adapter.URL_KEY: " https://from-env.test.invalid "}),
                             "https://from-env.test.invalid", "env wins over the manifest")
        ws = Path(tempfile.mkdtemp(prefix="r35-url-"))
        (ws / "state").mkdir()
        rdb._install_fake_capability(ws)
        env = {adapter.URL_KEY: "https://from-env.test.invalid"}
        store, _ = adapter.room_store(ws, environ=env)
        self.assertIn("https://from-env.test.invalid", store.client.argv)
        store, _ = adapter.room_store(ws, environ=env, collab_url="https://from-cli.test.invalid")
        self.assertIn("https://from-cli.test.invalid", store.client.argv, "the CLI tier wins over env")
        self.assertNotIn("https://from-env.test.invalid", store.client.argv)
        store, _ = adapter.room_store(ws, environ={})
        self.assertNotIn("--collab-url", store.client.argv)

    def test_core_instructions_and_sibling_skills_name_the_public_contract_not_the_skills_cli(self):
        for rel in ("CLAUDE.md", "AGENTS.md", "skills/context-reconstruct/SKILL.md", "skills/proactive-loop/SKILL.md",
                    "skills/proactive-loop/scripts/warn-already-triaged.py", "skills/self-diagnose/scripts/gather.sh",
                    "docs/proactive-loop-rationale.md", "docs/design-mediated-capability-layer.md"):
            text = (REPO / rel).read_text()
            self.assertNotIn("pq.py", text, f"{rel} names the skill's private CLI")
            self.assertNotIn("skills/pending-questions/scripts", text, rel)
        for rel in ("CLAUDE.md", "AGENTS.md"):
            text = (REPO / rel).read_text()
            for entry in ("scripts/ask-owner.py", "src/pending_questions_reader.py list", "src/check-pending-questions.py"):
                self.assertIn(entry, text, f"{rel} lacks {entry}")
        self.assertLessEqual(len((REPO / "CLAUDE.md").read_bytes()), 40960)

    def test_the_reader_cli_resolves_through_the_declared_store(self):
        ws = Path(tempfile.mkdtemp(prefix="r35-cli-"))
        for d in ("results", "state", f"hosts/{HOST}"):
            (ws / d).mkdir(parents=True)
        held = adapter.ask_owner("held?", urgency="durable", workspace=ws, host=HOST, store=None)
        cmd = [sys.executable, str(REPO / "src" / "pending_questions_reader.py"), "resolve", "--workspace", str(ws)]
        r = subprocess.run(cmd + ["ask-never-existed"], capture_output=True, text=True, timeout=120, env=ENV)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no held question ask-never-existed", r.stdout)
        r = subprocess.run(cmd + [held["ask_id"], "--answered"], capture_output=True, text=True, timeout=120, env=ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("recorded locally as Answered", r.stdout)
        self.assertEqual(sorted(pqs.Outbox(ws).closes()), [held["ask_id"]])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=ENV)
        self.assertEqual(r.returncode, 2)
        self.assertIn("resolve needs an ask id", r.stderr)


if __name__ == "__main__":
    unittest.main()
