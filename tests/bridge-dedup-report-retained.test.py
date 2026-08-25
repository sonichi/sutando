#!/usr/bin/env python3
"""An undelivered dedup report must not retire the question.

Two defects, one shape: the adapters decided the exchange was over by whether a
send was ATTEMPTED, not whether it landed. A `[deduped:]` with an empty holder
skipped recovery entirely, and a report whose send raised was archived anyway --
result and task both gone, so no later pass could retry and the asker was never
told. Both routes end with a real question permanently removed.

These drive the production `poll_results()` and the production `_dedup_recover`
wrappers; the failure arm is a send that genuinely raises, not a flag.

Run: python3 tests/bridge-dedup-report-retained.test.py
"""
from __future__ import annotations

import asyncio
import atexit
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Bridges resolve channel config AT IMPORT. Isolate before any exec_module runs,
# in this file: relying on another module's setup is not isolation.
_CFG = tempfile.mkdtemp(prefix="ccd-dedup-retained-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": []}))
(_cfg_discord / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

_cfg_telegram = Path(_CFG) / "channels" / "telegram"
_cfg_telegram.mkdir(parents=True, exist_ok=True)
(_cfg_telegram / "access.json").write_text(json.dumps({"allowFrom": []}))
(_cfg_telegram / ".env").write_text("TELEGRAM_BOT_TOKEN=test-token-not-real\n")

_cfg_slack = Path(_CFG) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text(json.dumps({"allowFrom": []}))
(_cfg_slack / ".env").write_text(
    "SLACK_BOT_TOKEN=xoxb-test-not-real\nSLACK_APP_TOKEN=xapp-test-not-real\n")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")


def _load_harness():
    """Reuse the poll-loop test's loader + SDK stubs rather than re-deriving them."""
    p = REPO / "tests" / "bridge-dedup-poll-loop.test.py"
    spec = importlib.util.spec_from_file_location("_dedup_harness", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load_harness()

from dedup_recovery import report_disposition  # noqa: E402


class DispositionContract(unittest.TestCase):
    """The rule itself: only a confirmed report retires the ask."""

    def test_report_retires_only_when_confirmed(self):
        self.assertEqual(report_disposition("report", True), "archive")
        self.assertEqual(report_disposition("report", False), "retain")
        self.assertEqual(report_disposition("report", None), "retain")

    def test_a_truthy_non_true_is_not_a_confirmation(self):
        """An adapter returning a dict or a string has not confirmed anything."""
        for v in ("ok", 1, {"ok": True}, [1]):
            self.assertEqual(report_disposition("report", v), "retain", repr(v))

    def test_non_sending_actions_are_terminal(self):
        self.assertEqual(report_disposition("honour", None), "archive")
        self.assertEqual(report_disposition("requeue", None), "archive")

    def test_unknown_action_fails_closed(self):
        """This decides whether a question survives; the unknown case retains."""
        self.assertEqual(report_disposition("defer", None), "retain")
        self.assertEqual(report_disposition("something-new", True), "retain")


class DiscordRealLoop(unittest.TestCase):
    """The production async branch, one pass, with a send that really fails."""

    def setUp(self):
        try:
            self.db = H._load("_rr_discord", "discord-bridge.py")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"discord-bridge not importable: {str(e)[:60]}")

    def _one_pass(self, td, marker, *, send_raises, orig=None):
        db = self.db
        orig = orig if orig is not None else H.ORIG + "dedup_requeue_count: 1\n"
        results, tasks = H._seed(db, td, "", orig)
        (results / f"{H.TID}.txt").write_text(marker)
        sent = []

        class _Chan:
            id = 4242

            async def send(self, text, **kw):
                if send_raises:
                    raise RuntimeError("channel unavailable")
                # Discord rejects a content-only send with no content; accepting
                # it here let a None payload read as delivered.
                if not text:
                    raise RuntimeError("Cannot send an empty message")
                sent.append(text)

        chan = _Chan()

        class _Client:
            def is_ready(self):
                return False

            async def fetch_channel(self, cid):
                return chan

        db.client = _Client()
        db._recovered_replies = {}
        db.pending_replies.clear()
        db.pending_replies[H.TID] = chan
        db.save_pending_replies = lambda *a, **k: None

        async def _sleep(_s):
            raise H._Stop()

        _orig = db.asyncio.sleep
        db.asyncio.sleep = _sleep
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                asyncio.run(db.poll_results())
        except H._Stop:
            pass
        finally:
            db.asyncio.sleep = _orig
        return {
            "sent": sent,
            "result_remaining": (results / f"{H.TID}.txt").exists(),
            "task_remaining": (tasks / f"{H.TID}.txt").exists(),
            "requeued": [p for p in tasks.glob("task-*.txt") if p.stem != H.TID],
            "log": buf.getvalue(),
        }

    def test_a_failed_report_retains_both_files(self):
        """The defect: the send raised and the question was archived regardless."""
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, f"[deduped: {H.HOLDER}]", send_raises=True)
            self.assertEqual(r["sent"], [], "control broken: the send did not fail")
            self.assertTrue(r["result_remaining"],
                            f"result archived after an undelivered report; log={r['log'][-400:]}")
            self.assertTrue(r["task_remaining"],
                            "task archived after an undelivered report — unrecoverable")

    def test_a_delivered_report_still_retires(self):
        """The fix must not strand every recovery; a told asker is terminal."""
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, f"[deduped: {H.HOLDER}]", send_raises=False)
            self.assertTrue(any("delivered nothing" in s for s in r["sent"]),
                            f"asker never told; sent={r['sent']}")
            self.assertFalse(r["result_remaining"], "retained a report that WAS delivered")

    def test_a_planner_that_raises_retains_both_files(self):
        """A planner exception proved nothing about the asker being answered.

        Before the fix `_dedup_recover` returned ("honour", None), which
        report_disposition archives -- retiring an unanswered question."""
        with tempfile.TemporaryDirectory() as td:
            db = self.db
            _orig_plan = db.plan_dedup_recovery
            db.plan_dedup_recovery = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("plan exploded"))
            try:
                r = self._one_pass(td, f"[deduped: {H.HOLDER}]", send_raises=False)
            finally:
                db.plan_dedup_recovery = _orig_plan
            self.assertIn("plan exploded", r["log"],
                          "control broken: the planner did not raise")
            self.assertEqual(r["sent"], [], "nobody should have been told")
            self.assertTrue(r["result_remaining"],
                            f"result archived after a failed plan; log={r['log'][-400:]}")
            self.assertTrue(r["task_remaining"],
                            "task archived after a failed plan -- the question is unrecoverable")

    def test_a_cross_channel_notice_that_raises_retains_both_files(self):
        """The sibling shape: _target prelabelled the action terminal, so an
        exception from the second-pass notify still archived the question."""
        with tempfile.TemporaryDirectory() as td:
            db = self.db
            _orig_t = db.dedup_cross_channel_target
            db.dedup_cross_channel_target = lambda *a, **k: 9999
            try:
                r = self._one_pass(td, f"[deduped: {H.HOLDER}]", send_raises=True)
            finally:
                db.dedup_cross_channel_target = _orig_t
            self.assertEqual(r["sent"], [], "control broken: the notify did not fail")
            self.assertTrue(r["result_remaining"],
                            f"result archived after a failed cross-channel notice; "
                            f"log={r['log'][-400:]}")
            self.assertTrue(r["task_remaining"],
                            "task archived after a failed cross-channel notice")

    def test_a_delivered_cross_channel_notice_still_retires(self):
        """Positive control: without it the two tests above pass by construction."""
        with tempfile.TemporaryDirectory() as td:
            db = self.db
            _orig_t = db.dedup_cross_channel_target
            db.dedup_cross_channel_target = lambda *a, **k: 9999
            try:
                r = self._one_pass(td, f"[deduped: {H.HOLDER}]", send_raises=False)
            finally:
                db.dedup_cross_channel_target = _orig_t
            self.assertTrue(r["sent"] and all(r["sent"]),
                            f"control broken: the notify never sent a real body ({r['sent']!r})")
            self.assertFalse(r["result_remaining"],
                             "retained a cross-channel notice that WAS delivered")

    def test_empty_holder_spellings_reach_recovery(self):
        """`[deduped:]` and `[deduped: ]` are dedup markers, not silent archives."""
        for marker in ("[deduped:]", "[deduped: ]"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as td:
                r = self._one_pass(td, marker, send_raises=False, orig=H.ORIG)
                self.assertEqual(
                    len(r["requeued"]), 1,
                    f"{marker!r} archived without a re-ask; log={r['log'][-400:]}")


class WrapperDisposition(unittest.TestCase):
    """Slack and Telegram own only the send; both must report its outcome."""

    def _wrapper(self, name, filename):
        try:
            return H._load(f"_rr_{name}", filename)
        except (Exception, SystemExit):  # noqa: BLE001 - optional SDK absent
            return None

    def test_slack_retains_when_the_post_fails(self):
        mod = self._wrapper("slack", "slack-bridge.py")
        if mod is None:
            self.skipTest("slack-bridge not importable")
        with tempfile.TemporaryDirectory() as td:
            H._seed(mod, td, "", H.ORIG + "dedup_requeue_count: 1\n")
            target = {"channel": "C1", "thread_ts": None, "access_tier": "owner"}
            mod._send_reply = lambda *a, **k: False
            self.assertEqual(mod._dedup_recover(H.TID, H.HOLDER, target), "retain")
            mod._send_reply = lambda *a, **k: True
            self.assertEqual(mod._dedup_recover(H.TID, H.HOLDER, target), "archive")

    def test_slack_retains_when_the_post_raises(self):
        mod = self._wrapper("slack", "slack-bridge.py")
        if mod is None:
            self.skipTest("slack-bridge not importable")
        with tempfile.TemporaryDirectory() as td:
            H._seed(mod, td, "", H.ORIG + "dedup_requeue_count: 1\n")

            def _boom(*a, **k):
                raise RuntimeError("slack down")

            mod._send_reply = _boom
            self.assertEqual(
                mod._dedup_recover(H.TID, H.HOLDER,
                                   {"channel": "C1", "access_tier": "owner"}), "retain")

    def test_telegram_reads_the_ok_flag_it_was_already_given(self):
        mod = self._wrapper("telegram", "telegram-bridge.py")
        if mod is None:
            self.skipTest("telegram-bridge not importable")
        with tempfile.TemporaryDirectory() as td:
            H._seed(mod, td, "", H.ORIG + "dedup_requeue_count: 1\n")
            mod.send_reply = lambda *a, **k: {"ok": False}
            self.assertEqual(mod._dedup_recover(H.TID, H.HOLDER, 77)[1], "retain")
            mod.send_reply = lambda *a, **k: {"ok": True}
            self.assertEqual(mod._dedup_recover(H.TID, H.HOLDER, 77)[1], "archive")

    def test_telegram_still_returns_the_id_to_route(self):
        """The disposition is additive; the re-ask must stay routable."""
        mod = self._wrapper("telegram", "telegram-bridge.py")
        if mod is None:
            self.skipTest("telegram-bridge not importable")
        with tempfile.TemporaryDirectory() as td:
            H._seed(mod, td, "", H.ORIG)
            new_id, disp = mod._dedup_recover(H.TID, H.HOLDER, 77)
            self.assertTrue(new_id, "re-ask id lost — its reply would be unroutable")
            self.assertEqual(disp, "archive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
