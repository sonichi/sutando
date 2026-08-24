#!/usr/bin/env python3
"""BEHAVIOURAL: the PARKED-before-archive recovery branch in poll_results.

Drives the real loop (one pass, sleep raises) against a result whose outbox
item is terminally PARKED but whose archive never ran — the crash window.
The pass must finish the archive; a claim merely HELD elsewhere must not.
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
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

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

_CFG = tempfile.mkdtemp(prefix="ccd-parked-archive-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": []}))

TID = "task-70a1b2c3d4e5f60718"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """Breaks the poll loop after exactly one pass."""


class ParkedArchiveTest(unittest.TestCase):
    def setUp(self):
        try:
            self.db = _load("_pa_discord", "discord-bridge.py")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"discord-bridge not importable: {str(e)[:60]}")
        self.drd = _load("_pa_drd", "discord_result_delivery.py")

    def _one_pass(self, td: str, prepare):
        import asyncio
        db = self.db
        results, tasks = Path(td) / "results", Path(td) / "tasks"
        (results / "archive").mkdir(parents=True)
        tasks.mkdir(parents=True)
        (tasks / f"{TID}.txt").write_text(
            f"id: {TID}\nsource: x\naccess_tier: owner\ntask: hi\n")
        (results / f"{TID}.txt").write_text("a reply body")
        prepare(results)
        db.RESULTS_DIR, db.TASKS_DIR = results, tasks
        db.ARCHIVE_RESULTS_DIR = results / "archive"

        class _Chan:
            id = 4242

            async def send(self, text, **kw):
                raise AssertionError("send must not be reached in these cases")

        chan = _Chan()

        class _Client:
            def is_ready(self):
                return False

            async def fetch_channel(self, cid):
                return chan

        db.client = _Client()
        db._recovered_replies = {}
        db.pending_replies.clear()
        db.pending_replies[TID] = chan
        db.save_pending_replies = lambda *a, **k: None

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
        return {"archived": (results / "archive" / f"{TID}.txt").exists()
                or bool(list((results / "archive").glob(f"{TID}*"))),
                "live": (results / f"{TID}.txt").exists(),
                "log": buf.getvalue()}

    def test_parked_pair_is_archived_on_restart(self):
        with tempfile.TemporaryDirectory() as td:
            def prepare(results):
                tok = self.drd.claim_for_send(results, TID)
                self.drd.failed_terminal(results, tok)  # parked, archive lost
            r = self._one_pass(td, prepare)
            self.assertFalse(r["live"],
                             f"parked result still live; log={r['log'][:300]}")
            self.assertIn("Parked (terminal)", r["log"])

    def test_held_claim_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            def prepare(results):
                self.drd.claim_for_send(results, TID)  # held, NOT parked
            r = self._one_pass(td, prepare)
            self.assertTrue(r["live"], "archived a merely-held claim")
            self.assertNotIn("Parked (terminal)", r["log"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
