#!/usr/bin/env python3
"""BEHAVIOURAL: the dedup recovery branch inside each bridge's delivery loop.

The wrappers are unit-tested elsewhere; this drives the real poll loops so the
call sites themselves are exercised — the routing of a requeue back into
`pending_replies`, and the in-channel report. Those lines are where a recovered
answer is either routed or lost, and nothing covered them before.

Single-iteration drivers, matching `approved-poll-path-and-quarantine`: patch
the loop's own sleep to raise, so exactly one pass runs.
"""
from __future__ import annotations

import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

try:
    import discord  # noqa: F401
except ImportError:
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(
        lambda: type("X", (), {"message_content": False})())})
    _d.Client = type("C", (), {"__init__": lambda self, **k: None,
                               "event": staticmethod(lambda fn: fn)})
    _d.File = type("F", (), {})
    _d.Message = type("M", (), {})
    _d.DMChannel = type("DM", (), {})
    sys.modules["discord"] = _d

# Always stub slack_bolt: the real App() performs a live auth.test at import.
_sb = types.ModuleType("slack_bolt")
_sb.App = type("App", (), {"__init__": lambda self, **kw: None,
                           "event": lambda self, name: (lambda fn: fn),
                           "client": None})
sys.modules["slack_bolt"] = _sb
sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
_sm = types.ModuleType("slack_bolt.adapter.socket_mode")
_sm.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter.socket_mode"] = _sm

_CFG = tempfile.mkdtemp(prefix="ccd-dedup-pollloop-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": []}))

_cfg_slack = Path(_CFG) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text(json.dumps({"allowFrom": []}))

TID = "task-633325612fbde6e777"
HOLDER = "task-22d83e59601f3a1fef"
ORIG = f"id: {TID}\nsource: x\naccess_tier: owner\ntask: What is AG2Space?\n"
DEDUP = f"[deduped: {HOLDER}]"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """Breaks the poll loop after exactly one pass."""


def _seed(mod, td: str, holder_body: str, orig: str = ORIG):
    results, tasks = Path(td) / "results", Path(td) / "tasks"
    (results / "archive").mkdir(parents=True)
    tasks.mkdir(parents=True)
    (results / "archive" / f"{HOLDER}-1785976425.txt").write_text(holder_body)
    (tasks / f"{TID}.txt").write_text(orig)
    (results / f"{TID}.txt").write_text(DEDUP)
    mod.RESULTS_DIR, mod.TASKS_DIR = results, tasks
    mod.ARCHIVE_RESULTS_DIR = results / "archive"
    state = Path(td) / "state"
    state.mkdir(exist_ok=True)
    if hasattr(mod, "DEDUP_ALIAS_FILE"):
        mod.DEDUP_ALIAS_FILE = state / "alias.json"
    return results, tasks


class DiscordPollLoopTest(unittest.TestCase):
    """`poll_results` — the async branch that routes or reports a recovery."""

    def setUp(self):
        try:
            self.db = _load("_pl_discord", "discord-bridge.py")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"discord-bridge not importable: {str(e)[:60]}")

    def _one_pass(self, td: str, holder_body: str, orig: str = ORIG, extra=None):
        import asyncio
        db = self.db
        results, tasks = _seed(db, td, holder_body, orig)
        sent: list = []

        class _Chan:
            id = 4242

            async def send(self, text, **kw):
                sent.append(text)

        chan = _Chan()

        class _Client:
            def is_ready(self):
                return False   # skip the heartbeat branch

            async def fetch_channel(self, cid):
                return chan

        db.client = _Client()
        db._recovered_replies = {}
        db.pending_replies.clear()
        db.pending_admitted_ms.clear()
        db.pending_replies[TID] = chan
        db.save_pending_replies = lambda *a, **k: None
        if extra:
            extra(tasks)

        async def _sleep(_s):
            raise _Stop()

        _orig_sleep = db.asyncio.sleep
        db.asyncio.sleep = _sleep
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                asyncio.run(db.poll_results())
        except _Stop:
            pass
        finally:
            db.asyncio.sleep = _orig_sleep
        return {"sent": sent, "pending": dict(db.pending_replies),
                "requeued": [p for p in tasks.glob("task-*.txt") if p.stem != TID],
                "log": buf.getvalue()}

    def test_requeue_is_routed_back_into_pending_replies(self):
        """The re-ask must be routed, or its answer has nowhere to go."""
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, "")
            self.assertEqual(len(r["requeued"]), 1, f"no re-ask written; log={r['log'][:300]}")
            new_id = r["requeued"][0].stem
            self.assertIn(new_id, r["pending"],
                          "re-ask not added to pending_replies — its reply is unroutable")

    def test_report_is_sent_in_channel(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, "", orig=ORIG + "dedup_requeue_count: 1\n")
            self.assertTrue(any("delivered nothing" in s for s in r["sent"]),
                            f"owner never told the ask could not be recovered; sent={r['sent']}")
            self.assertEqual(r["requeued"], [], "looped instead of reporting")

    def test_cross_channel_reject_stamps_admission(self):
        # holder lives in ANOTHER channel: first sighting re-queues here, and
        # the re-ask must carry an admitted_at stamp or the ager orphans it.
        with tempfile.TemporaryDirectory() as td:
            def extra(tasks):
                (tasks / f"{HOLDER}.txt").write_text(
                    f"id: {HOLDER}\nsource: discord\nchannel_id: 9999\ntask: other ask\n")
            r = self._one_pass(td, "", extra=extra)
            requeued = [p for p in r["requeued"] if p.stem != HOLDER]
            self.assertEqual(len(requeued), 1,
                             f"cross-channel reject did not re-queue; log={r['log'][:300]}")
            new_id = requeued[0].stem
            self.assertIn(new_id, r["pending"], "re-ask unroutable")
            self.assertIsInstance(self.db.pending_admitted_ms.get(new_id), int,
                                  "re-ask has no admitted_at stamp — ager will orphan it")
            self.assertIn("cross-channel reject", r["log"],
                          "took the same-channel path — branch under test not driven")

    def test_holder_that_answered_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, "the full answer")
            self.assertEqual(r["requeued"], [], "re-asked a dedup whose holder answered")
            self.assertEqual(r["sent"], [], "reported a dedup whose holder answered")


class SlackPollLoopTest(unittest.TestCase):
    """`result_watcher` — the same branch, in a thread rather than a coroutine."""

    def setUp(self):
        try:
            self.sb = _load("_pl_slack", "slack-bridge.py")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"slack-bridge not importable: {str(e)[:60]}")

    def _one_pass(self, td: str, holder_body: str):
        sb = self.sb
        _seed(sb, td, holder_body)
        sb._set_pending_reply(TID, {"channel": "C1", "access_tier": "owner"})
        sent: list = []
        sb._send_reply = lambda *a, **k: sent.append(a) or {"ok": True}

        def _sleep(_s):
            raise _Stop()

        _orig = sb.time.sleep
        sb.time.sleep = _sleep
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sb.result_watcher()
        except _Stop:
            pass
        except Exception:  # noqa: BLE001 - loop swallows and sleeps; sleep raises _Stop
            pass
        finally:
            sb.time.sleep = _orig
        tasks = Path(td) / "tasks"
        return {"sent": sent, "log": buf.getvalue(),
                "requeued": [p for p in tasks.glob("task-*.txt") if p.stem != TID]}

    def test_requeue_written_from_the_watcher_thread(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._one_pass(td, "")
            self.assertEqual(len(r["requeued"]), 1,
                             f"watcher did not recover the dedup; log={r['log'][:300]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
