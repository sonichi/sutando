#!/usr/bin/env python3
"""BEHAVIOURAL: the reply leg of result_watcher, bound to the shared outbox.

Drives the real loop (one pass; sleep raises) with only the Slack SDK stubbed.
Covers: delivered -> outbox DELIVERED + archive; crash-window restart (outbox
DELIVERED, archive lost) -> archived WITHOUT re-send; parked -> archived
without send; refusal -> kept for retry; ambiguous send -> parked, no retry.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

_tmp_home = tempfile.mkdtemp(prefix="slack-reply-outbox-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _tmp_home
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
_cfg = Path(_tmp_home) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text(json.dumps({"allowFrom": ["UOWNER"]}))

for name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode",
             "slack_sdk", "slack_sdk.errors"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        if name == "slack_bolt":
            m.App = type("App", (), {"__init__": lambda self, **kw: None,
                                     "event": lambda self, *a, **k: (lambda fn: fn),
                                     "client": types.SimpleNamespace()})
        if name == "slack_bolt.adapter.socket_mode":
            m.SocketModeHandler = type("SocketModeHandler", (),
                                       {"__init__": lambda self, *a, **kw: None})
        if name == "slack_sdk.errors":
            m.SlackApiError = type("SlackApiError", (Exception,), {})
        sys.modules[name] = m


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("slack_bridge_reply_outbox", REPO / "src" / "slack-bridge.py")
import slack_result_delivery as srd  # noqa: E402

TID = "task-slack-outbox-1"


class _Stop(Exception):
    """Breaks the poll loop after exactly one pass."""


class _Resp:
    status_code = 200
    data = {"ok": True, "ts": "1724.0001"}


class ReplyOutboxTest(unittest.TestCase):
    def _one_pass(self, td: str, post, prepare=None):
        results, tasks = Path(td) / "results", Path(td) / "tasks"
        (results / "archive").mkdir(parents=True)
        (tasks / "archive").mkdir(parents=True)
        (Path(td) / "state").mkdir()
        (tasks / f"{TID}.txt").write_text(f"id: {TID}\nsource: slack\ntask: hi\n")
        (results / f"{TID}.txt").write_text("a reply body")
        if prepare:
            prepare(results)

        calls: list[dict] = []

        def _post(**kw):
            calls.append(kw)
            return post()

        sb.REPO = Path(td)
        sb.RESULTS_DIR, sb.TASKS_DIR, sb.STATE_DIR = results, tasks, Path(td) / "state"
        sb.ARCHIVE_RESULTS_DIR = results / "archive"
        sb.ARCHIVE_TASKS_DIR = tasks / "archive"
        sb.app = types.SimpleNamespace(client=types.SimpleNamespace(chat_postMessage=_post))
        sb._atomic_write_pending_replies = lambda *a, **k: None
        sb.pending_replies.clear()
        sb.pending_replies[TID] = {"channel": "D0TEST", "thread_ts": None,
                                   "submitted_at": sb.time.time(),
                                   "access_tier": "owner"}

        def _sleep(_s):
            raise _Stop()

        orig_sleep = sb.time.sleep
        sb.time.sleep = _sleep
        try:
            with self.assertRaises(_Stop):
                sb.result_watcher()
        finally:
            sb.time.sleep = orig_sleep
        return {"calls": calls,
                "live": (results / f"{TID}.txt").exists(),
                "archived": bool(list((results / "archive").rglob(f"{TID}*"))),
                "status": __import__("outbox").item_status(
                    srd.result_backend(results).root, TID),
                "pending": TID in sb.pending_replies}

    def test_delivered_reply_confirms_and_archives(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, lambda: _Resp())
            self.assertEqual(len(r["calls"]), 1, "exactly one send")
            self.assertEqual(r["status"], "DELIVERED", "outbox receipt recorded")
            self.assertTrue(r["archived"] and not r["live"], "pair archived")
            self.assertFalse(r["pending"], "route retired")

    def test_crash_window_restart_never_resends(self):
        # outbox says DELIVERED but the archive never ran (crash between
        # confirm and archive): the pass archives WITHOUT calling Slack.
        with tempfile.TemporaryDirectory() as td:
            def prepare(results):
                tok = srd.claim_for_send(results, TID)
                srd.confirm(results, tok, "D0TEST")
            r = self._one_pass(td, lambda: _Resp(), prepare)
            self.assertEqual(len(r["calls"]), 0, "no re-send after restart")
            self.assertTrue(r["archived"] and not r["live"], "archive finished")

    def test_parked_pair_is_archived_without_send(self):
        with tempfile.TemporaryDirectory() as td:
            def prepare(results):
                tok = srd.claim_for_send(results, TID)
                srd.unknown(results, tok)  # parked, archive lost
            r = self._one_pass(td, lambda: _Resp(), prepare)
            self.assertEqual(len(r["calls"]), 0, "parked item is never re-sent")
            self.assertTrue(r["archived"] and not r["live"], "pair archived")

    def test_refused_reply_is_kept_for_retry(self):
        def _refuse():
            e = sys.modules["slack_sdk.errors"].SlackApiError("channel_not_found")
            e.response = types.SimpleNamespace(
                status_code=200, data={"ok": False, "error": "channel_not_found"})
            raise e
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, _refuse)
            self.assertTrue(r["live"] and not r["archived"], "kept for retry")
            self.assertTrue(r["pending"], "route kept")
            self.assertEqual(r["status"], "READY", "re-readied, attempt recorded")

    def test_ambiguous_send_parks_and_never_retries(self):
        def _timeout():
            raise TimeoutError("read timed out")
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, _timeout)
            self.assertEqual(r["status"], "PARKED",
                             "maybe-delivered reply parks (at-most-once bias)")
            self.assertTrue(r["live"], "pair archived on the NEXT pass by is_parked")


    def test_live_claim_holder_defers_the_send(self):
        # claim held by a LIVE incarnation (this pid): the pass must neither
        # send nor archive — the holder finishes or dies before we may act.
        with tempfile.TemporaryDirectory() as td:
            def prepare(results):
                self.assertIsNotNone(srd.claim_for_send(results, TID))
            r = self._one_pass(td, lambda: _Resp(), prepare)
            self.assertEqual(len(r["calls"]), 0, "sent past a live claim")
            self.assertTrue(r["live"] and not r["archived"], "kept for a later pass")
            self.assertTrue(r["pending"], "route kept")

    def test_escaped_raise_re_readies_via_outbox(self):
        # a raise ESCAPING _send_reply (provider raises park inside it): the
        # watcher's except must release the claim via failed() for retry.
        def _boom(*a, **k):
            raise RuntimeError("local failure outside the provider call")
        orig = sb._send_reply
        sb._send_reply = _boom
        try:
            with tempfile.TemporaryDirectory() as td:
                r = self._one_pass(td, lambda: _Resp())
        finally:
            sb._send_reply = orig
        self.assertEqual(r["status"], "READY", "failed() re-readied the item")
        self.assertTrue(r["live"] and not r["archived"], "kept for retry")
        self.assertTrue(r["pending"], "route kept")


class IngressReplaySkip(unittest.TestCase):
    def test_replayed_event_writes_no_second_task(self):
        # covers _write_task's dedup-hit return: same provider event delivered
        # twice must map to the SAME file and skip, never mint a second task.
        with tempfile.TemporaryDirectory() as td:
            tasks, results = Path(td) / "tasks", Path(td) / "results"
            tasks.mkdir(); results.mkdir()
            (Path(td) / "tasks" / "archive").mkdir()
            sb.TASKS_DIR, sb.RESULTS_DIR = tasks, results
            sb.ARCHIVE_TASKS_DIR = tasks / "archive"
            event = {"user": "UOWNER", "channel": "C1", "ts": "1724.0009",
                     "team": "T1", "channel_type": "im"}
            first = sb._write_task(event, "DM", "hello", "owner")
            self.assertIsNotNone(first, "first delivery admits")
            n_after_first = len(list(tasks.glob("task-*.txt")))
            second = sb._write_task(event, "DM", "hello", "owner")
            self.assertIsNone(second, "replay is skipped, not re-minted")
            self.assertEqual(len(list(tasks.glob("task-*.txt"))), n_after_first,
                             "replay wrote a second task file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
