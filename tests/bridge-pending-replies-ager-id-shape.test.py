#!/usr/bin/env python3
"""The pending_replies ager must not depend on task-id SHAPE (#3316 john r1).

Provider-derived ids (task-dc1~..., task-slT...~...) carry no parseable epoch;
the old ager parsed the id and silently skipped them, so their entries never
aged out — the exact 375-entry leak of 2026-05-05, reintroduced. Aging now
keys on a stored admitted_at. Pinned here: every id shape ages out; a fresh
entry of any shape survives; legacy string values are adopted, not immortal.

Run: python3 tests/bridge-pending-replies-ager-id-shape.test.py
Exit: 0 on pass, 1 on fail.
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

try:
    import discord  # noqa: F401
except ImportError:
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(lambda: type("X", (), {"message_content": False})())})
    _d.Client = type("C", (), {"__init__": lambda self, **k: None, "event": staticmethod(lambda fn: fn)})
    _d.File = type("F", (), {})
    _d.Message = type("M", (), {})
    sys.modules["discord"] = _d

_CFG = tempfile.mkdtemp(prefix="ccd-ager-shape-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg = Path(_CFG) / "channels" / "discord"
_cfg.mkdir(parents=True)
(_cfg / "access.json").write_text(json.dumps({"allowFrom": []}))
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

spec = importlib.util.spec_from_file_location("_ager_db", REPO / "src" / "discord-bridge.py")
MOD = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = MOD
spec.loader.exec_module(MOD)

WEEK_MS = 7 * 86400 * 1000
NOW_MS = int(time.time() * 1000)
OLD = NOW_MS - WEEK_MS - 3600_000     # 7d + 1h ago
FRESH = NOW_MS - 3600_000             # 1h ago


class AgerIdShape(unittest.TestCase):
    def _run_loader(self, payload: dict):
        td = tempfile.mkdtemp(prefix="ager-run-")
        self.addCleanup(shutil.rmtree, td, True)
        f = Path(td) / "pending_replies.json"
        f.write_text(json.dumps(payload))
        MOD.PENDING_REPLIES_FILE = f
        MOD.pending_admitted_ms.clear()
        out = MOD.load_pending_replies_from_disk()
        return out, f

    def test_provider_ids_age_out_by_admitted_at(self):
        out, _ = self._run_loader({
            "task-dc1~1409876543210987654": {"ch": "111", "at": OLD},
            "task-slT123~C456-1788000000.123": {"ch": "222", "at": OLD},
            "task-dc1~2222222222222222222": {"ch": "333", "at": FRESH},
        })
        self.assertNotIn("task-dc1~1409876543210987654", out)
        self.assertNotIn("task-slT123~C456-1788000000.123", out)
        self.assertIn("task-dc1~2222222222222222222", out)

    def test_legacy_epoch_id_string_keeps_original_clock(self):
        old_id = f"task-{OLD}"
        fresh_id = f"task-{FRESH}"
        out, _ = self._run_loader({old_id: "111", fresh_id: "222"})
        self.assertNotIn(old_id, out)
        self.assertIn(fresh_id, out)

    def test_legacy_unparseable_string_is_adopted_not_immortal(self):
        out, f = self._run_loader({"task-dc1~999": "111"})
        # first sight: kept, clock started (persisted with an at stamp)
        self.assertIn("task-dc1~999", out)
        stored = json.loads(f.read_text())["task-dc1~999"]
        self.assertIsInstance(stored, dict)
        self.assertIn("at", stored)
        # second load 8 days later would age it: simulate by rewriting at
        stored["at"] = OLD
        f.write_text(json.dumps({"task-dc1~999": stored}))
        MOD.pending_admitted_ms.clear()
        out2 = MOD.load_pending_replies_from_disk()
        self.assertNotIn("task-dc1~999", out2)

    def test_loader_returns_channel_strings_and_stamps_admitted(self):
        out, _ = self._run_loader({"task-dc1~7": {"ch": "999", "at": FRESH}})
        self.assertEqual(out["task-dc1~7"], "999")
        self.assertEqual(MOD.pending_admitted_ms["task-dc1~7"], FRESH)


if __name__ == "__main__":
    r = unittest.main(exit=False).result
    ok = r.wasSuccessful()
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
