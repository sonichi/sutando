#!/usr/bin/env python3
"""write_owner_activity(channel_id=…) records the routable channel id so the
core-supervisor relay's --active-from can escalate a blocked core to the exact
channel the owner is on. Exercises the channel_id branch in all three bridges'
write_owner_activity (the identical enrichment added for the ESCALATE layer).

Run: python3 tests/owner-activity-channel-id.test.py
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
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
# Redirect HOME + CLAUDE_CONFIG_DIR to a throwaway dir BEFORE seeding any channel
# .env, so importing the bridges never creates or dirties the real ~/.claude
# credential path in a contributor/CI environment. mkdtemp persists for the
# process; atexit removes it.
_TMP_HOME = tempfile.mkdtemp(prefix="owner-activity-test-home-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))
os.environ["HOME"] = _TMP_HOME
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_TMP_HOME) / ".claude")
# AG2_DEVICE_ENV outranks CLAUDE_CONFIG_DIR in _ag2space_access_path; this suite
# passes without it only while the operator's real map omits its fixture sender.
os.environ["AG2_DEVICE_ENV"] = ""
_ch_env = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / ".env"
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


class TestGatewayWriterChannelId(unittest.TestCase):
    """#2101 review (High #2): the AG2Space gateway bridge's _write_owner_activity
    must propagate channel_id, else the relay can't route an escalation back to
    the room the owner was last active in (degrades to macOS-only)."""

    def _load_gateway(self):
        pkg_root = _REPO / "packages" / "ag2-sparrow"
        if str(pkg_root) not in sys.path:
            sys.path.insert(0, str(pkg_root))
        from ag2_sparrow import remote_gateway_bridge as gw  # noqa: WPS433
        return gw

    def test_gateway_writer_records_channel_id(self):
        try:
            gw = self._load_gateway()
        except (Exception, SystemExit) as e:
            self.skipTest(f"gateway bridge not importable: {str(e)[:60]}")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "last-owner-activity.json"
            orig_file, orig_tier = gw.OWNER_ACTIVITY_FILE, gw.LOCAL_TIER
            gw.OWNER_ACTIVITY_FILE, gw.LOCAL_TIER = f, "owner"
            try:
                # channel_id present → recorded + bracket prefix stripped from summary
                gw._write_owner_activity({"task": "[AG2 @u] hi there",
                                          "source": "ag2space",
                                          "access_tier": "owner",
                                          "channel_id": "!room:ag2.space"})
                data = json.loads(f.read_text())
                self.assertEqual(data["channel"], "ag2space")
                self.assertEqual(data["channel_id"], "!room:ag2.space")
                self.assertEqual(data["summary"], "hi there")
                # channel_id omitted → key absent (back-compat with older readers)
                f.unlink()
                gw._write_owner_activity({"task": "[AG2 @u] no room", "source": "ag2space",
                                          "access_tier": "owner"})
                self.assertNotIn("channel_id", json.loads(f.read_text()))
            finally:
                gw.OWNER_ACTIVITY_FILE, gw.LOCAL_TIER = orig_file, orig_tier


if __name__ == "__main__":
    unittest.main()
