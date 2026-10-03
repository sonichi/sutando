#!/usr/bin/env python3
"""Owner pending questions in the owner's room database, the one store: a row is born
with its delivery record and confirmed before the outbox entry is deleted; the outbox is
written atomically before the room write, holds a question while the room is
unreachable, is replayed idempotently by the next pass (an incomplete row resumed) and
is listed once as "not yet in the room"; a host reads only its own rows; code never
writes the owner's Status and never reopens a closed row; the reminder sends nothing
unless asked. No real room is touched: the room-collab capability is a fake."""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import re
import runpy
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
pqa = importlib.import_module("pending_questions_ask")
adapter = importlib.import_module("pending_questions_room_db")
pqs = importlib.import_module("pending_questions_store")
reader = importlib.import_module("pending_questions_reader")
skill_roots = importlib.import_module("skill_roots")
parse_markers = importlib.import_module("result_markers").parse_markers

HOST = "test-host"
ROOM = "!ownerdm:test.invalid"
AGENT = "@agent:test.invalid"

# The fake capability: the two names the adapter imports, over a JSON file.
FAKE_ROOM_COLLAB = ("def resolve_token(x):\n    return 'tok'\n\n\n"
                    "def resolve_url(x):\n    return x or 'https://collab.test.invalid'\n")
FAKE_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager

    MAPS = ("dbs", "props", "rows", "cells", "views")

    class FakeDoc:
        def __init__(self, state=None):
            state = state or {}
            self.maps = {m: dict(state.get(m) or {}) for m in MAPS}
            self.bodies = dict(state.get("bodies") or {})
            self.writes = []

        @property
        def database(self):
            return {m: dict(v) for m, v in self.maps.items()}

        async def put_database(self, writes):
            self.writes.append(writes)
            for name, entries in writes.items():
                for k, v in entries.items():
                    if v is None:
                        self.maps[name].pop(k, None)
                    else:
                        self.maps[name][k] = v
                    if name == "rows" and v is not None and k not in self.bodies:
                        self.bodies[k] = ""
            return sum(len(e) for e in writes.values())

        def row_body(self, db, row):
            return self.bodies.get(db + "|" + row)

        async def put_row_body(self, db, row, text, append=False):
            k = db + "|" + row
            if k not in self.maps["rows"]:
                raise LookupError("no row")
            self.bodies[k] = (self.bodies.get(k, "") + text) if append else text
            return len(self.bodies[k])

        async def settle(self, seconds=1.0):
            return None

        def state(self):
            return {**self.maps, "bodies": self.bodies}

    @asynccontextmanager
    async def open_room_collab(url, room, token, kind="doc", **kw):
        if os.environ.get("FAKE_ROOM_FAIL") or "unreachable" in url:
            raise ConnectionError("service refused the socket")
        path = os.environ["FAKE_ROOM_STATE"]
        try:
            state = json.load(open(path))
        except (OSError, ValueError):
            state = {}
        assert kind == "db" and room == state.get("_room", room)
        doc = FakeDoc(state)
        yield doc
        with open(path, "w") as fh:
            json.dump({**doc.state(), "_room": room}, fh)
''')

_spec = importlib.util.spec_from_loader("fake_client", loader=None)
fake_client = importlib.util.module_from_spec(_spec)
exec(FAKE_CLIENT, fake_client.__dict__)


class InProcClient:
    """A DbClient running the adapter's `apply` on an in-memory FakeDoc."""

    def __init__(self, doc=None, fail=None, fail_ops=()):
        self.doc, self.fail, self.fail_ops, self.calls = doc or fake_client.FakeDoc(), fail, fail_ops, []

    def _do(self, req):
        self.calls.append(req["op"])
        if self.fail or req["op"] in self.fail_ops:
            raise pqs.StoreError(self.fail or f"{req['op']} refused")
        try:
            return asyncio.run(adapter.apply(self.doc, req, AGENT, 1_000, "https://link/row"))
        except (LookupError, ValueError) as e:
            raise pqs.StoreError(str(e)) from None

    def add_row(self, schema, row, cells, body):
        return self._do({"op": "add_row", "schema": schema, "row": row, "cells": cells, "body": body})

    def row(self, schema, row):
        return self._do({"op": "row", "schema": schema, "row": row})

    def rows(self, schema):
        return self._do({"op": "rows", "schema": schema})

    def set_cells(self, schema, row, cells):
        return self._do({"op": "set_cells", "schema": schema, "row": row, "cells": cells})

    def set_body(self, schema, row, body):
        return self._do({"op": "set_body", "schema": schema, "row": row, "body": body})

    def guarded(self, schema, row, cells, expect):
        return self._do({"op": "guarded", "schema": schema, "row": row, "cells": cells, "expect": expect})


def _cpq(ws):
    spec = importlib.util.spec_from_file_location("cpq", REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_remind.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.WORKSPACE = Path(ws)
    m.RESULTS_DIR = Path(ws) / "results"
    m.LAST_NOTIFY_FILE = Path(ws) / "state" / "last-pq-notify"
    m.VOICE_LOG = Path(ws) / "logs" / "voice-agent.log"
    return m


def _install_fake_capability(ws: Path, owner_dm=ROOM, identity=AGENT):
    d = ws / "skills" / "room-collab" / "scripts"
    d.mkdir(parents=True)
    (d / "room_collab.py").write_text(FAKE_ROOM_COLLAB)
    (d / "room_collab_client.py").write_text(FAKE_CLIENT)
    if owner_dm is not None:
        (ws / "state" / "owner-routing.json").write_text(
            json.dumps({"owner_dm": owner_dm, "identity": identity}))


class _Ws(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pq-roomdb-"))
        for d in ("results", "state", f"hosts/{HOST}"):
            (self.ws / d).mkdir(parents=True)
        os.environ["SUTANDO_HOST_LABEL"] = HOST
        self.addCleanup(os.environ.pop, "SUTANDO_HOST_LABEL", None)

    def q(self, ask_id="ask-1", question="Merge #12 now?", **kw):
        return pqs.Question(ask_id, question, kw.pop("context", None), 1_790_000_000.0, **kw)

    def db(self, client=None, host=HOST):
        return pqs.RoomDbStore(client or InProcClient(), lock=self.ws / "state" / "pq-db.lock", host=host)

    def outbox(self):
        return sorted(p.name for p in (self.ws / "state" / "pending-questions-outbox").glob("*"))

    def ask(self, question="q?", store=None, **kw):
        """The skill's ask (reconcile, queue, hold, row); `store=None` holds it in the outbox."""
        return adapter.ask_owner(question, urgency="durable", workspace=self.ws, host=HOST, store=store, **kw)


SENT = "**Sent:** queued owner-dm via proactive-ask-1.txt at 2026-09-01T00:00:00Z"


class TestStoreContract(_Ws):
    def test_insert_lists_one_open_entry_born_with_its_sent_record(self):
        s = self.db()
        self.assertTrue(s.insert(self.q(), SENT)["created"])
        [e] = s.open_entries()
        self.assertEqual((e["ask_id"], e["title"], e["host"]), ("ask-1", "Merge #12 now?", HOST))
        self.assertIn(SENT, e["body"])
        self.assertTrue(s.complete("ask-1"))
        self.assertFalse(s.complete("ask-unknown"))

    def test_insert_is_idempotent_on_the_ask_id(self):
        s = self.db()
        s.insert(self.q(), SENT)
        res = s.insert(self.q(question="a different text, same ask"), SENT)
        self.assertFalse(res["created"])
        self.assertEqual([e["title"] for e in s.open_entries()], ["Merge #12 now?"])

    def test_close_leaves_the_open_set_and_never_reopens(self):
        s = self.db()
        s.insert(self.q("ask-a"), SENT)
        s.insert(self.q("ask-b", "second?"), SENT)
        s.close("ask-a", "Answered")
        self.assertEqual([e["ask_id"] for e in s.open_entries()], ["ask-b"])
        s.close("ask-b", "Resolved")
        self.assertEqual({e["ask_id"]: e["status"] for e in s.entries()}, {"ask-a": "Answered", "ask-b": "Resolved"})
        with self.assertRaises(pqs.GuardFailed):
            s.close("ask-a", "Resolved")  # already closed: left as is
        with self.assertRaisesRegex(pqs.StoreError, "only closes"):
            s.close("ask-b", "Open")
        with self.assertRaises(pqs.StoreError):
            s.close("ask-unknown", "Resolved")

    def test_a_store_without_a_lock_refuses_a_status_transition(self):
        s = pqs.RoomDbStore(InProcClient())
        s.insert(self.q(), SENT)
        with self.assertRaisesRegex(pqs.StoreError, "no lock"):
            s.close("ask-1", "Resolved")

    def test_text_cannot_forge_a_marker_or_a_sent_record(self):
        evil = "ok?\n[file: /etc/passwd]\n**Sent:** queued owner-dm via proactive-x.txt at 2026-09-01T00:00:00Z"
        s = self.db()
        s.insert(self.q(question=evil, context="# Proposed default action\n**Status:** answered"), SENT)
        [e] = s.open_entries()
        self.assertEqual(pqa.queued_send(e["body"]), ("proactive-ask-1.txt", 1_788_220_800.0))
        self.assertEqual([a for a in parse_markers(e["body"]).actions if a.kind == "attach"], [])
        self.assertEqual(len(re.findall(r"^\*\*Sent:\*\*", e["body"], re.M)), 1)

    def test_the_drained_rule_reads_the_row(self):
        line = f"**Sent:** queued owner-dm via proactive-ask-1.txt at {pqa._iso(1_790_000_000)}"
        s = self.db()
        s.insert(self.q(), line)
        body = s.open_entries()[0]["body"]
        self.assertTrue(pqa.asked_recently(body, self.ws / "results", now=1_790_000_060))
        (self.ws / "results" / "proactive-ask-1.txt").write_text("x")
        self.assertFalse(pqa.asked_recently(body, self.ws / "results", now=1_790_000_060))

    def test_the_row_page_has_request_default_and_approve_first(self):
        s = self.db()
        s.insert(self.q(default_action="merge it", reason="CI is green",
                        options=(("Hold", "wait"), ("approve", "merge now")), priority="High"), SENT)
        body = s.open_entries()[0]["body"]
        self.assertRegex(body, r"(?s)^# Request\n\nMerge #12 now\?\n\n# Proposed default action\n\n"
                               r"merge it — CI is green\n\n\*\*Approve\*\* -> merge now\n\*\*Hold\*\* -> wait")
        cells = next(iter(s.client.rows(pqs.DB_SCHEMA)))["cells"]
        self.assertEqual((cells["status"], cells["priority"], cells["ask_id"]), ("open", "high", "ask-1"))

    def test_code_never_writes_the_owners_status(self):
        writes = []

        class Recording(InProcClient):
            def _do(self, req):
                res = super()._do(req)
                if req["op"] == "set_cells" or (req["op"] == "guarded" and (res or {}).get("written")):
                    writes.append(req["cells"].get("status"))
                return res
        s = self.db(Recording())
        s.insert(self.q(), SENT)
        s.close("ask-1", "Answered")
        s.clear("ask-1", ["closed"])
        self.assertEqual({w for w in writes if w is not None}, set())


class TestAdapterServe(_Ws):
    def test_the_database_is_created_once_and_rows_carry_their_creation(self):
        c = InProcClient()
        s = self.db(c)
        s.insert(self.q(), SENT)
        s.insert(self.q("ask-2"), SENT)
        self.assertEqual(len([w for w in c.doc.writes if "dbs" in w]), 1)
        self.assertEqual(c.doc.maps["dbs"]["pendingq"]["name"], "Pending questions")
        self.assertEqual({e["asked_at"] for e in s.entries()}, {1.0})

    def test_rows_of_other_databases_and_other_hosts_are_not_read(self):
        doc = fake_client.FakeDoc({"rows": {"other|r1": {"order": 1}}, "cells": {"other|r1|status": {"v": "open"}}})
        self.db(InProcClient(doc), host="host-b").insert(self.q("ask-b"), SENT)
        self.assertEqual(self.db(InProcClient(doc)).entries(), [])


class TestDiscoveryAndScriptClient(_Ws):
    def test_discovery_needs_the_capability_the_room_and_an_identity(self):
        self.assertIsNone(adapter.room_store(self.ws, environ={})[0])
        _install_fake_capability(self.ws, owner_dm=None)
        store, why = adapter.room_store(self.ws, environ={})
        self.assertIsNone(store)
        self.assertIn("owner_dm", why)
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps({"owner_dm": ROOM}))
        self.assertIsNone(adapter.room_store(self.ws, environ={})[0])
        store, where = adapter.room_store(self.ws, environ={"AG2SPACE_USER_ID": AGENT})
        self.assertEqual(where, ROOM)
        self.assertIn(AGENT, store.client.argv)
        self.assertEqual(store.lock, self.ws / "state" / "pending-questions-db.lock")
        self.assertEqual(store.link, f"https://collab.test.invalid/#/room/{ROOM}?surface=db&page=pendingq")

    def test_the_script_client_round_trips_through_the_adapter(self):
        _install_fake_capability(self.ws)
        state = self.ws / "fake-room.json"
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(state)}):
            store, _ = adapter.room_store(self.ws, environ={})
            res = store.insert(self.q(), SENT)
            self.assertTrue(store.complete("ask-1"))
            store.close("ask-1", "Resolved")
            self.assertEqual(store.status_of("ask-1"), "Resolved")
        self.assertEqual(res["link"], store.link)
        self.assertEqual(json.loads(state.read_text())["_room"], ROOM)

    def test_every_script_failure_is_a_store_error(self):
        bad = self.ws / "bad.py"
        for src in ("import sys; sys.exit(3)", "print('not json')", "print('{\"ok\": false, \"error\": \"nope\"}')",
                    "import time; time.sleep(5)"):
            bad.write_text(src)
            c = pqs.ScriptDbClient([sys.executable, str(bad)], timeout=1)
            with self.assertRaises(pqs.StoreError, msg=src):
                c.rows(pqs.DB_SCHEMA)
        with self.assertRaises(pqs.StoreError):
            pqs.ScriptDbClient([str(self.ws / "missing")]).rows(pqs.DB_SCHEMA)

    def test_the_collab_url_env_points_the_adapter_at_a_service_for_a_test_run(self):
        """An unreachable one is a clean failure: the question lands in the outbox."""
        _install_fake_capability(self.ws)
        env = {"PENDING_QUESTIONS_COLLAB_URL": "https://unreachable.test.invalid",
               "FAKE_ROOM_STATE": str(self.ws / "fake-room.json")}
        with mock.patch.dict(os.environ, env):
            store, _ = adapter.room_store(self.ws, environ=env)
            self.assertIn("--collab-url", store.client.argv)
            self.assertTrue(store.link.startswith("https://unreachable.test.invalid/"))
            out = self.ask("q?", store=store)
        self.assertIn("service refused the socket", out["db_error"])
        self.assertEqual(len(self.outbox()), 1)


class TestEdges(_Ws):
    def _serve(self, req, env):
        if not (self.ws / "skills").exists():
            _install_fake_capability(self.ws)
        argv = ["serve", "--room", ROOM, "--user-id", AGENT,
                "--skill-scripts", str(self.ws / "skills" / "room-collab" / "scripts")]
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(req))), \
                contextlib.redirect_stdout(out), mock.patch.object(adapter, "SETTLE_SEC", 0):
            rc = adapter.main(argv)
        return rc, json.loads(out.getvalue())

    def test_serve_answers_each_op_and_reports_failures_as_json(self):
        env = {"FAKE_ROOM_STATE": str(self.ws / "room.json")}
        base = {"schema": pqs.DB_SCHEMA, "row": "r1"}
        self.assertEqual(self._serve({**base, "op": "add_row", "cells": {"name": "q"}, "body": "b"}, env)[1]["ok"], True)
        self.assertEqual(self._serve({**base, "op": "set_cells", "cells": {"name": "q2"}}, env)[0], 0)
        self.assertEqual(self._serve({**base, "op": "set_body", "body": "new"}, env)[0], 0)
        rc, reply = self._serve({**base, "op": "row"}, env)
        self.assertEqual((reply["result"]["cells"]["name"], reply["result"]["body"]), ("q2", "new"))
        self.assertIsInstance(reply["result"]["created"], int)
        for bad in ({**base, "op": "frobnicate"}, {**base, "op": "set_body", "row": "nope", "body": "x"},
                    {**base, "op": "stamp", "token": "absent", "replacement": "x"}):
            rc, reply = self._serve(bad, env)
            self.assertEqual((rc, reply["ok"]), (1, False), bad)
        rc, reply = self._serve({**base, "op": "rows"}, {**env, "FAKE_ROOM_FAIL": "1"})
        self.assertEqual(rc, 1)
        self.assertIn("service refused the socket", reply["error"])

    def test_a_held_lock_refuses_rather_than_waits_forever(self):
        s = self.db()
        s.insert(self.q(), SENT)
        s.lock.mkdir(parents=True)
        with mock.patch.object(pqs.ledger, "LOCK_WAIT_SEC", 0.1):
            with self.assertRaisesRegex(pqs.StoreError, "could not acquire"):
                s.close("ask-1", "Resolved")
        self.assertTrue(s.lock.exists())

    def test_an_unreadable_outbox_file_is_named_and_left_in_place(self):
        ob = pqs.Outbox(self.ws)
        ob.dir.mkdir(parents=True)
        (ob.dir / "ask-bad.json").write_text("not json")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ob.entries(), [])
            self.assertEqual(ob.flush(self.db()), ([], []))
        self.assertIn("ask-bad.json unreadable", err.getvalue())
        self.assertEqual(self.outbox(), ["ask-bad.json"])

    def test_ask_owner_cli_resolves_the_workspace_and_refuses_a_bad_option(self):
        cli = str(REPO / "scripts" / "ask-owner.py")
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                mock.patch.object(skill_roots, "declared_script", return_value=None), \
                mock.patch.object(sys, "argv", [cli, "q?", "--urgency", "durable"]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            with contextlib.suppress(SystemExit):
                runpy.run_path(cli, run_name="__main__")
        self.assertIn(str(self.ws / "state" / "ask-owner"), out.getvalue(), "the resolved workspace, core's record")
        with mock.patch.object(sys, "argv", [cli, "q?", "--option", "no-equals"]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as e:
                runpy.run_path(cli, run_name="__main__")
        self.assertEqual(e.exception.code, 2)
        self.assertIn("Label=what it does", err.getvalue())


class TestAskOwner(_Ws):
    def test_the_row_is_born_with_its_record_and_the_outbox_entry_is_deleted(self):
        db = self.db()
        db.link = "https://db/page"
        out = self.ask("Merge #12?", store=db, default_action="merge")
        self.assertIsNone(out["db_error"])
        self.assertIsNone(out["outbox"])
        self.assertEqual(self.outbox(), [])
        [e] = db.open_entries()
        self.assertEqual(e["ask_id"], out["ask_id"])
        self.assertIn(f"via {out['proactive_file']} at", e["body"])
        body = (self.ws / "results" / out["proactive_file"]).read_text()
        self.assertIn("set its Status on the row: https://db/page", body)
        self.assertIn("Pending questions database", body)
        self.assertEqual("\n".join(pqa.report_lines(out)).count("recorded:"), 1)
        self.assertIn("row: https://link/row", pqa.report_lines(out))

    def test_the_outbox_entry_is_written_atomically_before_the_room_write(self):
        seen = {}
        ob = pqs.Outbox(self.ws)

        class Watching(InProcClient):
            def add_row(self, schema, row, cells, body):
                seen["outbox_at_write"] = sorted(p.name for p in ob.dir.iterdir())
                return super().add_row(schema, row, cells, body)
        out = self.ask("q?", store=self.db(Watching()))
        self.assertEqual(seen["outbox_at_write"], [f"{out['ask_id']}.json"], "no temp file, the entry whole")
        self.assertEqual(self.outbox(), [])

    def test_a_database_failure_leaves_the_outbox_entry_and_says_so(self):
        out = self.ask("q?", store=self.db(InProcClient(fail="service refused")))
        self.assertIn("service refused", out["db_error"])
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])
        self.assertIsNotNone(out["proactive_file"], "the owner is still asked")
        lines = "\n".join(pqa.report_lines(out))
        self.assertIn("recorded: OUTBOX", lines)
        self.assertIn("ROOM DATABASE WRITE FAILED", lines)
        rec = json.loads((self.ws / "state" / "pending-questions-outbox" / f"{out['ask_id']}.json").read_text())
        self.assertEqual(rec["question"]["question"], "q?")
        self.assertIn(f"via {out['proactive_file']} at", rec["sent"])

    def test_without_a_store_the_outbox_holds_it_and_the_reader_lists_it_once(self):
        out = self.ask("Solo?")
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])
        self.assertNotIn("Pending questions database", (self.ws / "results" / out["proactive_file"]).read_text())
        g = reader.gather(self.ws, adapter=Path(adapter.__file__))
        self.assertEqual([(i["ask_id"], i["in_room"]) for i in g["waiting"]], [(out["ask_id"], False)])
        self.assertEqual((g["done"], g["pending_close"]), (0, []))
        self.assertIn("listing the local outbox only", g["notes"][0])
        with mock.patch.object(skill_roots, "declared_script", return_value=None):
            g = reader.gather(self.ws)
        self.assertEqual((g["unavailable"], g["done"], g["waiting"]), (True, None, []), "core alone has no store to read")

    def test_the_next_ask_files_the_held_question_once(self):
        held = self.ask("First?")  # no store: held
        db = self.db()
        out = self.ask("Second?", store=db)
        self.assertEqual(out["reconcile"]["flushed"], [held["ask_id"]])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(sorted(e["ask_id"] for e in db.open_entries()), sorted([held["ask_id"], out["ask_id"]]))
        held_row = next(e for e in db.entries() if e["ask_id"] == held["ask_id"])
        self.assertIn(f"via {held['proactive_file']} at", held_row["body"])
        self.assertEqual(self.ask("Third?", store=db)["reconcile"]["flushed"], [])
        self.assertEqual(len(db.entries()), 3)

    def test_crash_before_the_room_write_outbox_present_row_absent_next_pass_creates_it(self):
        class Killed(BaseException):
            pass

        class Dies(InProcClient):
            def add_row(self, *a, **k):
                raise Killed()
        with self.assertRaises(Killed):
            self.ask("Ship it?", store=self.db(Dies()))
        [name] = self.outbox()
        db = self.db()
        self.assertEqual(db.entries(), [])
        flushed, errors = pqs.Outbox(self.ws).flush(db)
        self.assertEqual((flushed, errors), ([name[:-5]], []))
        self.assertEqual(self.outbox(), [])
        [e] = db.open_entries()
        self.assertIn("Ship it?", e["body"])

    def test_an_incomplete_row_is_resumed_by_the_replay_and_listed_once(self):
        doc = fake_client.FakeDoc()
        real, state = doc.put_row_body, {"fail": True}

        async def dies_once(*a, **k):
            if state["fail"]:
                state["fail"] = False
                raise ConnectionError("socket closed between row and body")
            return await real(*a, **k)
        doc.put_row_body = dies_once
        db = self.db(InProcClient(doc))
        out = self.ask("Ship the release?", store=db)
        self.assertIn("socket closed", out["db_error"])
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])
        [row] = db.entries()
        self.assertTrue(row["incomplete"])
        self.assertEqual(db.open_entries(), [], "an incomplete row is never listed as open")
        self.assertEqual([(i["ask_id"], i["in_room"]) for i in pqs.outbox_items(self.ws)], [(out["ask_id"], False)])
        self.assertEqual(reconcile_items(self, db), [(out["ask_id"], True)])
        self.assertEqual(self.outbox(), [])
        [row] = db.entries()
        self.assertFalse(row["incomplete"])
        self.assertIn(f"via {out['proactive_file']} at", row["body"])
        self.assertEqual(reconcile_items(self, db), [(out["ask_id"], True)])

    def test_a_body_that_landed_but_whose_mark_clear_was_lost_is_finished(self):
        doc = fake_client.FakeDoc()
        real, calls = doc.put_database, {"n": 0}

        async def loses_the_clear(writes):
            cells = writes.get("cells") or {}
            if any(k.endswith("|recovery") and v is None for k, v in cells.items()) and calls["n"] == 0:
                calls["n"] += 1
                raise ConnectionError("ack lost after the body landed")
            return await real(writes)
        doc.put_database = loses_the_clear
        db = self.db(InProcClient(doc))
        out = self.ask("q?", store=db)
        self.assertIsNotNone(out["db_error"])
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(pqs.Outbox(self.ws).flush(db)[0], [out["ask_id"]])
        [row] = db.entries()
        self.assertEqual((row["status"], row["incomplete"]), ("Open", False))
        self.assertEqual(row["body"].count("**Sent:**"), 1, "the body that landed is kept, not rewritten")

    def test_a_lost_acknowledgement_after_a_complete_row_is_a_deleted_entry_not_a_second_row(self):
        class AckLost(InProcClient):
            def add_row(self, *a, **k):
                super().add_row(*a, **k)
                raise pqs.StoreError("reply lost")
        doc = fake_client.FakeDoc()
        out = self.ask("q?", store=self.db(AckLost(doc)))
        self.assertEqual(len(self.outbox()), 1)
        db = self.db(InProcClient(doc))
        self.assertEqual(pqs.Outbox(self.ws).flush(db), ([out["ask_id"]], []))
        self.assertEqual(len(db.entries()), 1)

    def test_an_outbox_entry_is_kept_while_the_room_stays_unreachable(self):
        out = self.ask("q?", store=self.db(InProcClient(fail="down")))
        flushed, errors = pqs.Outbox(self.ws).flush(self.db(InProcClient(fail="still down")))
        self.assertEqual(flushed, [])
        self.assertIn(out["ask_id"], errors[0])
        self.assertEqual(self.outbox(), [f"{out['ask_id']}.json"])

    def test_a_failed_proactive_write_is_recorded_as_failed_in_the_row(self):
        (self.ws / "results").rmdir()
        (self.ws / "results").write_text("not a directory")
        db = self.db()
        out = self.ask("still asked?", store=db)
        self.assertIsNone(out["proactive_file"])
        [e] = db.open_entries()
        self.assertIn("**Sent:** FAILED", e["body"])
        self.assertIsNone(pqa.sent_at(e["body"]), "a failed send is not a send")
        self.assertIn("sent: FAILED", "\n".join(pqa.report_lines(out)))

    def test_the_first_ask_with_a_database_says_where_it_lives_and_only_once(self):
        db = self.db()
        bodies = [(self.ws / "results" / self.ask(q, store=db)["proactive_file"]).read_text() for q in ("A?", "B?")]
        self.assertIn("Pending questions database", bodies[0])
        self.assertIn("not on a schedule", bodies[0])
        self.assertNotIn("Pending questions database", bodies[1])


def reconcile_items(tc, db):
    """One pass as the adapter runs it, on the in-process store: (ask id, in room) per item."""
    rec = pqs.reconcile_pending(db, tc.ws, HOST)
    tc.assertEqual(rec["errors"], [])
    rows = [(e["ask_id"], True) for e in db.open_entries()]
    held = {a for a, _ in rows}
    return rows + [(i["ask_id"], False) for i in pqs.outbox_items(tc.ws) if i["ask_id"] not in held]


class TestGather(_Ws):
    """The adapter over the fake capability: the explicit pass (reconcile), then the read-only
    gather lists rows + outbox, once each."""

    def _gather(self, env=None):
        state = self.ws / "fake-room.json"
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(state), **(env or {})}):
            rec = adapter.reconcile_pass(self.ws, environ={})
            g = adapter.gather(self.ws, environ={})
            g["notes"] = [f"reconcile: FAILED — {e}" for e in rec["errors"]] + g["notes"]
            return g

    def test_rows_and_held_questions_are_listed_once_each_and_counted_alike(self):
        held = self.ask("Held?")  # no store yet
        _install_fake_capability(self.ws)
        state = self.ws / "fake-room.json"
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(state)}):
            store, _ = adapter.room_store(self.ws, environ={})
            asked = self.ask("Asked?", store=store)
            store.insert(self.q("ask-done", "Done?"), SENT)
            store.close("ask-done", "Resolved")
            g = self._gather()
            self.assertEqual(sorted((i["ask_id"], i["in_room"]) for i in g["waiting"]),
                             sorted([(held["ask_id"], True), (asked["ask_id"], True)]))
            self.assertEqual((g["done"], g["notes"], g["unavailable"]), (1, [], False))
            self.assertEqual(self.outbox(), [])
            full = {"open": 2, "done": 1, "pending_close": 0, "unavailable": False, "reason": None}
            self.assertEqual(adapter.count(self.ws), full)
            self.assertEqual(len(adapter.waiting(self.ws)), 2)
            self.assertEqual(reader.count(self.ws, adapter=Path(adapter.__file__)), full)

    def test_an_unreachable_room_lists_the_outbox_and_says_unknown_not_zero(self):
        _install_fake_capability(self.ws)
        with mock.patch.dict(os.environ, {"FAKE_ROOM_FAIL": "1", "FAKE_ROOM_STATE": str(self.ws / "r.json")}):
            store, _ = adapter.room_store(self.ws, environ={})
            out = self.ask("q?", store=store)
            g = adapter.gather(self.ws, environ={})
        self.assertEqual([(i["ask_id"], i["in_room"]) for i in g["waiting"]], [(out["ask_id"], False)])
        self.assertEqual((g["unavailable"], g["done"]), (True, None))
        self.assertIn("service refused", g["reason"])
        self.assertTrue(any("UNAVAILABLE" in n and "service refused" in n for n in g["notes"]), g["notes"])

    def test_resolve_closes_the_row_and_files_a_held_question_first(self):
        held = self.ask("Held?")
        _install_fake_capability(self.ws)
        with mock.patch.dict(os.environ, {"FAKE_ROOM_STATE": str(self.ws / "r.json")}):
            ok, msg = adapter.resolve(self.ws, held["ask_id"], "Answered")
            self.assertTrue(ok, msg)
            self.assertIn("-> Answered", msg)
            self.assertEqual(adapter.count(self.ws)["open"], 0)
            self.assertEqual(adapter.count(self.ws)["done"], 1)
            ok, msg = adapter.resolve(self.ws, held["ask_id"], "Resolved")
            self.assertFalse(ok)
            self.assertIn("not changed", msg)
            self.assertFalse(adapter.resolve(self.ws, "ask-nope", "Resolved")[0])

    def test_without_the_capability_resolve_records_the_close_locally_for_a_held_question_only(self):
        held = self.ask("Held?")
        ok, msg = adapter.resolve(self.ws, held["ask_id"], "Resolved")
        self.assertTrue(ok, msg)
        self.assertIn("recorded locally as Resolved", msg)
        self.assertIn("no room capability installed", msg)
        self.assertTrue((self.ws / "state" / "pending-questions-outbox" / "closed" / f"{held['ask_id']}.json").exists())
        ok, msg = adapter.resolve(self.ws, "ask-1", "Resolved")
        self.assertFalse(ok)
        self.assertIn("no held question ask-1 and no room row was ever confirmed here", msg)
        self.assertFalse((self.ws / "state" / "pending-questions-outbox" / "closed" / "ask-1.json").exists())


class TestTwoHostsOneRoom(_Ws):
    def test_neither_host_reads_or_touches_the_others_rows(self):
        doc = fake_client.FakeDoc()
        a = pqs.RoomDbStore(InProcClient(doc), lock=self.ws / "state" / "a", host="host-a")
        b = pqs.RoomDbStore(InProcClient(doc), lock=self.ws / "state" / "b", host="host-b")
        a.insert(self.q("ask-a1", "host A's question?"), SENT)
        b.insert(self.q("ask-b1", "host B's question?"), SENT)
        self.assertEqual([e["ask_id"] for e in a.open_entries()], ["ask-a1"])
        self.assertEqual([e["ask_id"] for e in b.open_entries()], ["ask-b1"])
        with self.assertRaises(pqs.StoreError):
            b.close("ask-a1", "Resolved")
        self.assertEqual(a.status_of("ask-a1"), "Open")
        self.assertEqual(pqs.reconcile_pending(b, self.ws, "host-b")["errors"], [])
        self.assertEqual(len(doc.maps["rows"]), 2)

    def test_two_hosts_with_one_ask_id_get_two_rows(self):
        doc = fake_client.FakeDoc()
        a = pqs.RoomDbStore(InProcClient(doc), lock=self.ws / "state" / "a", host="host-a")
        b = pqs.RoomDbStore(InProcClient(doc), lock=self.ws / "state" / "b", host="host-b")
        self.assertTrue(a.insert(self.q("ask-x"), SENT)["created"])
        self.assertTrue(b.insert(self.q("ask-x"), SENT)["created"])
        b.close("ask-x", "Resolved")
        self.assertEqual(sorted((e["host"], e["status"]) for e in a.entries() + b.entries()),
                         [("host-a", "Open"), ("host-b", "Resolved")])

    def test_host_labels_that_slug_alike_still_get_two_row_keys(self):
        a = pqs.RoomDbStore(InProcClient(), host="Host-A")
        b = pqs.RoomDbStore(InProcClient(), host="host-a")
        self.assertNotEqual(a._rid("ask-1"), b._rid("ask-1"))


class TestConcurrentReplicas(_Ws):
    """Two replicas of one database each run a write from a stale view, then merge
    the way a Yjs map does: per key, the higher client id wins."""

    @staticmethod
    def _merge(base, a, b, a_wins):
        for name in a.maps:
            for k in set(base[name]) | set(a.maps[name]) | set(b.maps[name]):
                ca, cb = a.maps[name].get(k) != base[name].get(k), b.maps[name].get(k) != base[name].get(k)
                pick = a if ca and (a_wins or not cb) else b
                v = pick.maps[name].get(k)
                for d in (a, b):
                    if v is None:
                        d.maps[name].pop(k, None)
                    else:
                        d.maps[name][k] = v

    def test_an_owner_decision_survives_a_concurrent_close_in_either_merge_order(self):
        for owner_status in ("resolved", "answered"):
            for agent_wins in (True, False):
                with self.subTest(owner=owner_status, agent_wins=agent_wins):
                    owner, agent = fake_client.FakeDoc(), fake_client.FakeDoc()
                    seed = self.db(InProcClient(owner))
                    seed.insert(self.q("ask-x"), SENT)
                    key = f"pendingq|{seed._rid('ask-x')}|status"
                    agent.maps, base = json.loads(json.dumps(owner.maps)), json.loads(json.dumps(owner.maps))
                    agent.bodies = dict(owner.bodies)
                    owner.maps["cells"][key] = {"v": owner_status, "updated": 9, "by": "o"}
                    self.db(InProcClient(agent)).close("ask-x", "Resolved")  # its view still says Open
                    self._merge(base, agent, owner, a_wins=agent_wins)
                    got = {pqs.effective_status(adapter._cells(d.maps, "pendingq", seed._rid("ask-x")))
                           for d in (owner, agent)}
                    self.assertEqual(got, {owner_status.capitalize()})

    def test_a_stale_close_beside_the_owners_status_is_cleared_by_the_next_pass(self):
        doc = fake_client.FakeDoc()
        db = self.db(InProcClient(doc))
        db.insert(self.q("ask-x"), SENT)
        db.close("ask-x", "Resolved")  # from a stale replica
        key = f"pendingq|{db._rid('ask-x')}|"
        doc.maps["cells"][key + "status"] = {"v": "answered", "updated": 9, "by": "owner"}
        self.assertEqual(db.status_of("ask-x"), "Answered")
        self.assertEqual(pqs.reconcile_pending(db, self.ws, HOST)["errors"], [])
        self.assertNotIn(key + "closed", doc.maps["cells"])
        self.assertEqual(doc.maps["cells"][key + "status"]["v"], "answered")

    @unittest.skipUnless(importlib.util.find_spec("pycrdt"), "pycrdt is not installed")
    def test_the_same_race_on_real_yjs_replicas_in_both_client_id_orders(self):
        from pycrdt import Doc, Map

        class Replica:
            def __init__(self, client_id):
                self.ydoc = Doc(client_id=client_id)
                self.maps = {m: self.ydoc.get(m, type=Map) for m in fake_client.MAPS}

            @property
            def database(self):
                return {m: {k: dict(v) if hasattr(v, "keys") else v for k, v in y.items()}
                        for m, y in self.maps.items()}

            async def put_database(self, writes):
                with self.ydoc.transaction():
                    for name, entries in writes.items():
                        for k, v in entries.items():
                            if v is None:
                                self.maps[name].pop(k, None)
                            else:
                                self.maps[name][k] = v
                return 0

            def row_body(self, db, row):
                return "page"

            async def put_row_body(self, *a, **k):
                return 0

        def sync(a, b):
            ua, ub = a.ydoc.get_update(b.ydoc.get_state()), b.ydoc.get_update(a.ydoc.get_state())
            b.ydoc.apply_update(ua)
            a.ydoc.apply_update(ub)

        for owner_id, agent_id in ((1, 2), (2, 1)):
            with self.subTest(owner_id=owner_id, agent_id=agent_id):
                owner, agent = Replica(owner_id), Replica(agent_id)
                seed = self.db(InProcClient(owner))
                seed.insert(self.q("ask-x"), SENT)
                rid = seed._rid("ask-x")
                sync(owner, agent)
                asyncio.run(owner.put_database({"cells": {f"pendingq|{rid}|status":
                                                          {"v": "resolved", "updated": 9, "by": "o"}}}))
                self.db(InProcClient(agent)).close("ask-x", "Answered")  # still sees Open
                sync(owner, agent)
                got = {pqs.effective_status(adapter._cells(r.database, "pendingq", rid)) for r in (owner, agent)}
                self.assertEqual(got, {"Resolved"})


class TestReminder(_Ws):
    def _main(self, db, *argv):
        cpq = _cpq(self.ws)
        cpq.notify_macos = lambda count, titles: True
        cpq.voice_client_connected = lambda: False
        def fake_gather(ws, environ=None):
            return {"waiting": [pqs.waiting_item(e["ask_id"], e["title"], e["body"], e["asked_at"], True)
                                for e in db.open_entries()] + pqs.outbox_items(ws),
                    "done": 0, "notes": [], "store": "x", "unavailable": False, "reason": None}
        buf = io.StringIO()
        with mock.patch.object(adapter, "gather", fake_gather), \
                mock.patch.object(sys, "argv", ["check-pending-questions.py", *argv]), \
                contextlib.redirect_stdout(buf):
            cpq.main()
        return buf.getvalue()

    def test_a_flagless_or_reconcile_only_run_lists_and_sends_nothing(self):
        db = self.db()
        self.ask("fresh?", store=db)
        held = self.ask("held?")
        for flags in ((), ("--reconcile-only",), ("--force",)):
            out = self._main(db, *flags)
            self.assertIn("2 pending questions; nothing sent", out)
            self.assertIn(f"[{held['ask_id']}] held? (not yet in the room)", out)
            self.assertEqual([p for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-pending-q-")], [])

    def test_notify_reminds_rows_and_held_questions_once_each(self):
        db = self.db()
        self.ask("fresh?", store=db)
        self.ask("held?")
        out = self._main(db, "--notify")
        self.assertIn("Notified: 2 pending questions", out)
        [f] = [p for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-pending-q-")]
        self.assertIn("• held?", f.read_text())
        self.assertIn("Pending questions database", f.read_text())

    def test_a_drained_row_is_not_due_and_an_undrained_one_is(self):
        db = self.db()
        out = self.ask("fresh?", store=db)
        cpq = _cpq(self.ws)
        [item] = [pqs.waiting_item(e["ask_id"], e["title"], e["body"], None, True) for e in db.open_entries()]
        self.assertEqual(len(cpq.due_for_reminder([item])), 1, "undrained: still due")
        (self.ws / "results" / out["proactive_file"]).unlink()
        self.assertEqual(cpq.due_for_reminder([item]), [], "drained within the hour: quiet")
        self.assertIn("(sent) 1 pending questions", self._main(db, "--notify"))

    def test_the_store_adapter_flag_loads_the_injected_file(self):
        fake = self.ws / "fake_adapter.py"
        fake.write_text("def reconcile_pass(ws):\n"
                        "    return {'flushed': [], 'moved': [], 'closed': [], 'errors': ['its pass ran']}\n"
                        "def gather(ws):\n"
                        "    return {'waiting': [], 'done': 0, 'notes': ['from the flag'], 'store': 'f'}\n")
        cpq = _cpq(self.ws)
        with mock.patch.object(sys, "argv", ["check-pending-questions.py", "--store-adapter", str(fake)]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            cpq.main()
        self.assertIn("0 pending questions; nothing sent", out.getvalue())
        self.assertIn("from the flag", err.getvalue())
        self.assertIn("reconcile: FAILED — its pass ran", err.getvalue(), "the injected adapter is read through its pass")


class TestDelegation(unittest.TestCase):
    """Each write has one owner; core never names the capability or the skill."""

    SKILL = "skills/pending-questions/scripts/"

    def src(self, rel):
        return (REPO / rel).read_text()

    def test_core_never_names_the_capability_or_the_skill(self):
        for rel in ("src/pending_questions_reader.py", "src/local_record.py", "src/check-pending-questions.py",
                    "scripts/ask-owner.py"):
            self.assertNotRegex(self.src(rel), r"room[-_]collab|room[-_]commons", rel)
            self.assertNotRegex(self.src(rel), r"pending_questions_room_db|skills/pending-questions", rel)

    def test_the_skills_ask_queues_then_holds_then_writes_the_row_and_only_with_a_held_record(self):
        s = self.src(self.SKILL + "pending_questions_room_db.py")
        self.assertIn("core_ask.queue_question(", s)
        self.assertIn('if out["outbox"] is None:', s)
        self.assertLess(s.index("core_ask.queue_question("), s.index('if out["outbox"] is None:'))
        self.assertLess(s.index('if out["outbox"] is None:'), s.index("write_question(q, store, out[\"sent_line\"])"))
        self.assertNotRegex(s, r"\bledger\.|FileStore|pending-questions\.md")
        core = self.src(self.SKILL + "pending_questions_ask.py")
        self.assertIn("Outbox(ws).save(out[\"question\"], out[\"sent_line\"], now)", core)
        self.assertLess(core.index("write_proactive(ws"), core.index("Outbox(ws).save("), "queue, then hold")

    def test_the_store_confirms_the_exact_ask_id_before_the_outbox_entry_goes(self):
        s = self.src(self.SKILL + "pending_questions_store.py")
        self.assertIn("if not store.complete(q.ask_id):", s)
        self.assertLess(s.index("store.complete(q.ask_id)"), s.index("self.delete(q.ask_id)"))
        self.assertIn('if cells.get("ask_id") != ask_id or not self.owns(cells):', s)
        self.assertNotRegex(s, r"FileStore|def resync|def stamp|set_status|re\.sub\(r\"\[\^A-Za-z0-9_-\]\"")

    def test_the_adapter_writes_only_through_the_documents_own_calls(self):
        s = self.src(self.SKILL + "pending_questions_room_db.py")
        writes = set(re.findall(r"await doc\.(\w+)\(", s))
        self.assertEqual(writes, {"put_database", "put_row_body", "settle"})
        self.assertNotRegex(s, r"write_text|os\.replace|pending-questions\.md")

    def test_the_reminder_reads_the_siblings_pass_and_writes_no_row(self):
        s = self.src(self.SKILL + "pending_questions_remind.py")
        self.assertIn("reader.reconcile_then_gather(WORKSPACE, adapter)", s, "one core-owned pass: reconcile_pass, then gather")
        self.assertNotIn("reconcile=True", s, "the contract's two entry points, no private keyword")
        self.assertNotRegex(s, r"(?<!sys\.path)\.(insert|insert_raw|close|clear)\(|PQ_FILE|personal_path|pending-questions\.md")
        self.assertIn('if "--notify" not in argv', s)
        shim = self.src("src/check-pending-questions.py")
        self.assertIn('hasattr(mod, "remind")', shim)
        self.assertNotRegex(shim, r"notify_macos|proactive-pending-q|write_text")

    def test_ask_owner_cli_injects_what_discovery_found(self):
        seen = {}
        store = object()

        def _ask(question, **kw):
            seen.update(kw)
            return {"db_error": None, "record": "x", "heading": "## x", "outbox": None, "link": None,
                    "proactive_file": "p", "where": "w", "send_error": None, "macos": None, "reconcile": None}
        fake = mock.Mock(room_store=lambda ws: (store, ROOM), ask_owner=_ask, report_lines=lambda out: ["recorded: x"])
        ws = tempfile.mkdtemp()
        with mock.patch.object(reader, "_adapter", return_value=(fake, "fake.py")), \
                contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location("ask_owner_cli", REPO / "scripts" / "ask-owner.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            self.assertEqual(m.main(["q?", "--workspace", ws]), 0)
        self.assertIs(seen["store"], store)

    def test_the_migration_script_is_gone(self):
        self.assertFalse((REPO / "scripts" / "pending-questions-migrate.py").exists())


if __name__ == "__main__":
    unittest.main()
