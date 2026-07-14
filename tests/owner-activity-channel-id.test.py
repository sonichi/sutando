#!/usr/bin/env python3
"""write_owner_activity(channel_id=…) records the routable channel id so the
core-supervisor relay's --active-from can escalate a blocked core to the exact
channel the owner is on. Exercises the channel_id branch in all three bridges'
write_owner_activity (the identical enrichment added for the ESCALATE layer).

Run: python3 tests/owner-activity-channel-id.test.py
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

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

# discord-bridge does `discord.Client(...)` at module load — stub `discord` (and
# seed its .env token) so the bridge imports for coverage even where discord.py
# isn't installed. Mirrors tests/discord-bridge-file-markers.test.py.
try:
    import discord  # noqa: F401
except ImportError:
    _stub = types.ModuleType("discord")
    _stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    _stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
    _stub.File = type("File", (), {})
    _stub.Message = type("Message", (), {})
    sys.modules["discord"] = _stub
_ch_env = Path.home() / ".claude" / "channels" / "discord" / ".env"
if not _ch_env.exists():
    _ch_env.parent.mkdir(parents=True, exist_ok=True)
    _ch_env.write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

# (module-name, filename, module-load env so the bridge doesn't exit on a
# missing token). Tokens are placeholders — no API call fires; only
# write_owner_activity (stdlib-only) is exercised.
_BRIDGES = [
    ("discord_bridge_wa", "discord-bridge.py", {"DISCORD_BOT_TOKEN": "test-x"}),
    ("telegram_bridge_wa", "telegram-bridge.py", {"TELEGRAM_BOT_TOKEN": "test-x"}),
    ("slack_bridge_wa", "slack-bridge.py",
     {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}),
]


def _load(modname, filename, env):
    for k, v in env.items():
        os.environ.setdefault(k, v)
    spec = importlib.util.spec_from_file_location(modname, _REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # __main__ guard → polling loop never starts
    return mod


class TestOwnerActivityChannelId(unittest.TestCase):
    def _exercise(self, mod):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "last-owner-activity.json"
            orig_file, orig_dir = mod.OWNER_ACTIVITY_FILE, mod.STATE_DIR
            mod.OWNER_ACTIVITY_FILE, mod.STATE_DIR = f, Path(td)
            try:
                # channel_id given → recorded (as str)
                mod.write_owner_activity("discord", "hello", channel_id=12345)
                data = json.loads(f.read_text())
                self.assertEqual(data["channel_id"], "12345")
                self.assertEqual(data["channel"], "discord")
                # channel_id omitted → key absent (back-compat with older readers)
                mod.write_owner_activity("discord", "hi again")
                self.assertNotIn("channel_id", json.loads(f.read_text()))
            finally:
                mod.OWNER_ACTIVITY_FILE, mod.STATE_DIR = orig_file, orig_dir

    def test_all_bridges_record_channel_id(self):
        exercised = 0
        for modname, filename, env in _BRIDGES:
            try:
                mod = _load(modname, filename, env)
            except (Exception, SystemExit) as e:
                # Optional bridge SDK absent locally (discord.py / slack_bolt exit
                # at import). CI has them installed → the bridge loads and
                # write_owner_activity is exercised for the coverage gate. `continue`
                # (not skipTest) so one un-loadable bridge never stops the others.
                print(f"  [skip] {filename}: {str(e)[:60]}")
                continue
            with self.subTest(bridge=filename):
                self._exercise(mod)
            exercised += 1
        self.assertGreater(exercised, 0, "no bridge was importable — cannot verify")


if __name__ == "__main__":
    unittest.main()
