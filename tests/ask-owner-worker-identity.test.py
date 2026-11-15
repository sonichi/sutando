#!/usr/bin/env python3
"""A pool worker that acts as its own agent asks the owner as itself.

- In a worker session (SUTANDO_WORKER_ID) whose own identity is enabled, ask-owner's text ask is
  queued under the name `result_publish.worker_published_path` gives it (`.by-<worker>`), so that
  worker's own lane delivers it; the store adapter and the no-store path both do.
- The core's session, and a worker with no identity or a disabled one, queue unstamped names.
- The question records who asked (the worker's agent id when stamped) in the outbox and the row.
- A decision card stays on the core's path: no stamped text is sent beside it.
Temp workspaces and fakes only; nothing here reads the live workspace or a real token.
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for p in (REPO / "src", REPO / "skills" / "pending-questions" / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import connections as C
import pending_questions_ask as pqa
import pending_questions_room_db as room_db
import pending_questions_store as pqs
import proactive_routing as pr
import result_publish

W = "7c700e11" + "0" * 24
AGENT = "@helper:alpha-fixture.test"
HOST = "test-host"

_spec = importlib.util.spec_from_file_location("ask_owner_cli", REPO / "scripts" / "ask-owner.py")
ask_owner_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask_owner_cli)


class FakeDb:
    """A DbClient over a dict: rows keep the cells and body they were born with."""

    def __init__(self):
        self.data = {}

    def add_row(self, schema, row, cells, body):
        self.data[row] = {"cells": dict(cells), "body": body}
        return {"created": True}

    def row(self, schema, row):
        return self.data.get(row)

    def rows(self, schema):
        return [{"row": k, **v} for k, v in self.data.items()]

    def set_cells(self, schema, row, cells):
        self.data[row]["cells"].update(cells)

    def set_body(self, schema, row, body):
        self.data[row]["body"] = body

    def guarded(self, schema, row, cells, expect):
        self.data[row]["cells"].update(cells)
        return {"ok": True}


class _Ws(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ask-owner-worker-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "workspace"
        for d in ("results", "state", f"hosts/{HOST}"):
            (self.ws / d).mkdir(parents=True)
        for patcher in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "config"),
                                                     "SUTANDO_HOST_LABEL": HOST}),
                        mock.patch("workspace_default.resolve_workspace", return_value=self.ws)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("SUTANDO_WORKER_ID", None)
        self.record(own=True)

    def record(self, own=True, **extra):
        base = {"gateway_url": "https://chat.alpha-fixture.test/relay", "primary": False, "added_by": "owner",
                "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity",
                "server": "alpha-fixture.test"}
        rows = [{**base, "instance": "alpha", "token_ref": C.token_ref_for("alpha"), "primary": True,
                 "agent_mxid": "@core:alpha-fixture.test"}]
        if own:
            inst = C.worker_instance_for("alpha-fixture.test", W)
            rows.append({**base, "instance": inst, "token_ref": C.token_ref_for(inst), "worker": W,
                         "agent_mxid": AGENT, **extra})
        C.record_path(self.ws).write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rows}))

    def as_worker(self, worker=W):
        return mock.patch.dict(os.environ, {"SUTANDO_WORKER_ID": worker} if worker else {})

    def proactive(self):
        return sorted(p.name for p in (self.ws / "results").glob("proactive-*"))

    def outbox_questions(self):
        return [json.loads(p.read_text())["question"]
                for p in (self.ws / "state" / "pending-questions-outbox").glob("*.json")]


class StoreAdapterAsk(_Ws):
    def ask(self, store=None, **kw):
        return room_db.ask_owner("Merge #12 now?", urgency="durable", workspace=self.ws, host=HOST,
                                 store=store, **kw)

    def test_a_worker_with_its_own_enabled_identity_asks_stamped_and_the_row_names_its_agent(self):
        db = FakeDb()
        store = pqs.RoomDbStore(db, lock=self.ws / "state" / "pq-db.lock", host=HOST)
        with self.as_worker():
            out = self.ask(store=store)
        [name] = self.proactive()
        self.assertEqual(pr.proactive_worker(name), W, name)
        self.assertEqual(out["proactive_file"], name)
        self.assertTrue(pr.lane_claims_proactive(name, W))
        self.assertFalse(pr.lane_claims_proactive(name), "no core lane sends it")
        [row] = db.data.values()
        self.assertEqual(row["cells"].get("asked_by"), AGENT)
        self.assertIn(name, row["body"], "the row's delivery record names the stamped file")

    def test_the_outbox_holds_who_asked_and_replays_it_into_the_row(self):
        with self.as_worker():
            self.ask(store=None)
        [q] = self.outbox_questions()
        self.assertEqual(q["asked_by"], AGENT)
        cells = []
        fake = FakeDb()
        fake.add_row = lambda schema, row, c, body: cells.append(c) or {"created": True}
        pqs.RoomDbStore(fake, lock=self.ws / "state" / "l", host=HOST).insert(pqs.Question.from_dict(q), "x")
        self.assertEqual(cells[0]["asked_by"], AGENT)

    def test_the_cores_session_asks_unstamped_and_names_no_asker(self):
        db = FakeDb()
        out = self.ask(store=pqs.RoomDbStore(db, lock=self.ws / "state" / "pq-db.lock", host=HOST))
        [name] = self.proactive()
        self.assertIsNone(pr.proactive_worker(name))
        self.assertEqual(out["question"]["asked_by"], None)
        self.assertNotIn("asked_by", next(iter(db.data.values()))["cells"])

    def test_a_worker_with_no_identity_or_a_disabled_one_asks_unstamped(self):
        for label, kw in (("none", {"own": False}), ("disabled", {"disabled": True}), ("parked", {"parked": True})):
            with self.subTest(label):
                for p in (self.ws / "results").glob("*"):
                    p.unlink()
                self.record(**kw)
                with self.as_worker():
                    out = self.ask()
                [name] = self.proactive()
                self.assertIsNone(pr.proactive_worker(name), name)
                self.assertIsNone(out["question"]["asked_by"])

    def test_the_stamp_comes_from_result_publishs_helper(self):
        calls = []

        def fake(path, environ=None, workspace=None):
            calls.append((Path(path).name, workspace))
            return Path(path).with_name("proactive-from-helper.txt")

        with mock.patch.object(result_publish, "worker_published_path", fake):
            out = self.ask()
        self.assertEqual(self.proactive(), ["proactive-from-helper.txt"])
        self.assertEqual(out["proactive_file"], "proactive-from-helper.txt")
        self.assertTrue(calls and all(ws == self.ws for _, ws in calls), calls)

    def test_an_asker_that_cannot_be_read_never_blocks_the_ask(self):
        with self.as_worker(), mock.patch.object(C, "proactive_author_agent", side_effect=OSError("unreadable")):
            out = self.ask()
        [name] = self.proactive()
        self.assertEqual((pr.proactive_worker(name), out["question"]["asked_by"]), (W, None))

    def test_a_decision_card_stays_the_cores_and_no_stamped_text_goes_beside_it(self):
        (self.ws / "state" / "last-owner-activity.json").write_text(json.dumps({"channel": "ag2space"}))
        from hitl.manager import HitlManager, HitlStore
        manager = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        with self.as_worker():
            out = pqa.queue_question("Merge it?", workspace=self.ws, host=HOST, default_action="merge",
                                     options=[("Hold", "wait")], manager=manager)
            self.assertEqual(pqa.create_card(out), pqa.CARD_STORED)
        self.assertEqual(self.proactive(), [], "the card is the ask; no text is queued")
        self.assertEqual(out["question"]["asked_by"], AGENT)


class NoStoreAsk(_Ws):
    def run_cli(self):
        err, outp = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(outp), \
                mock.patch.object(ask_owner_cli, "declared", return_value=None):
            rc = ask_owner_cli.main(["Ship it?", "--workspace", str(self.ws), "--urgency", "durable"])
        self.assertEqual(rc, 0, err.getvalue())
        [rec] = [json.loads(p.read_text()) for p in (self.ws / "state").rglob("ask-*.json")] or [None]
        return self.proactive(), rec

    def test_a_worker_with_its_own_identity_queues_stamped_and_records_its_agent(self):
        with self.as_worker():
            names, rec = self.run_cli()
        self.assertEqual([pr.proactive_worker(n) for n in names], [W])
        self.assertEqual((rec["queued"], rec["asked_by"]), (names[0], AGENT))

    def test_the_stamp_comes_from_result_publishs_helper(self):
        self.assertIs(ask_owner_cli.worker_published_path, result_publish.worker_published_path)
        fake = mock.Mock(side_effect=lambda path, workspace=None: Path(path).with_name("proactive-from-helper.txt"))
        with mock.patch.object(ask_owner_cli, "worker_published_path", fake):
            names, rec = self.run_cli()
        self.assertEqual((names, rec["queued"]), (["proactive-from-helper.txt"], "proactive-from-helper.txt"))
        self.assertEqual(fake.call_args.kwargs["workspace"], self.ws)

    def test_an_asker_that_cannot_be_read_never_blocks_the_ask(self):
        with self.as_worker(), mock.patch.object(C, "proactive_author_agent", side_effect=OSError("unreadable")):
            names, rec = self.run_cli()
        self.assertEqual(([pr.proactive_worker(n) for n in names], rec["asked_by"]), ([W], None))

    def test_the_core_and_a_worker_without_an_enabled_identity_queue_unstamped(self):
        for label, kw, worker in (("core", {}, ""), ("none", {"own": False}, W), ("disabled", {"disabled": True}, W)):
            with self.subTest(label):
                shutil.rmtree(self.ws / "results")
                (self.ws / "results").mkdir()
                shutil.rmtree(self.ws / "state" / "ask-owner", ignore_errors=True)
                self.record(**kw)
                with self.as_worker(worker):
                    if not worker:
                        os.environ.pop("SUTANDO_WORKER_ID", None)
                    names, rec = self.run_cli()
                self.assertEqual([pr.proactive_worker(n) for n in names], [None])
                self.assertIsNone(rec["asked_by"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
