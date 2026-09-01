#!/usr/bin/env python3
"""BEHAVIOURAL: `_supervise_loop` must not restart a loop that opted out.

`poll_progress` returns immediately when `SUTANDO_PROGRESS_STREAM` is off — its
docstring promises "never loops; zero overhead, zero risk". But `_supervise_loop`
treats *any* return as a crash ("a poll loop returning is itself unexpected"), so
on the DEFAULT configuration it re-entered and re-returned every
POLL_LOOP_RESTART_SEC forever: ~17k log lines/day and a needless task, on the
live owner-facing bridge.

Two arms, because the fix must be narrow:
  - disabled loop  -> supervisor stops, silently.
  - loop that returns for any OTHER reason -> still restarted (real failures must
    keep being supervised).
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

# The real App() performs a live auth.test at import.
_sb = types.ModuleType("slack_bolt")
_sb.App = type("App", (), {"__init__": lambda self, **kw: None,
                           "event": lambda self, name: (lambda fn: fn),
                           "client": None})
sys.modules["slack_bolt"] = _sb
sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
_sm = types.ModuleType("slack_bolt.adapter.socket_mode")
_sm.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter.socket_mode"] = _sm

_CFG = tempfile.mkdtemp(prefix="ccd-progress-supervise-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
# Seeded with literal channel names, not a loop variable: the hermetic-bridge lint
# traces the path-segment chain statically and cannot prove a computed segment.
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": []}))

_cfg_slack = Path(_CFG) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text(json.dumps({"allowFrom": []}))

# The defect only reproduces with the feature OFF, which is the shipped default.
os.environ["SUTANDO_PROGRESS_STREAM"] = "0"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _SleptTwice(Exception):
    """Escape hatch: proves the supervisor entered its restart cycle."""


class ProgressLoopDisabledTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load("discord_bridge_supervise", "discord-bridge.py")
        self.sleeps = 0

        async def _counting_sleep(_secs):
            self.sleeps += 1
            # One restart is already the bug; a second guarantees we never hang.
            if self.sleeps >= 2:
                raise _SleptTwice
        self._orig_sleep = self.mod.asyncio.sleep
        self.mod.asyncio.sleep = _counting_sleep

    def tearDown(self):
        self.mod.asyncio.sleep = self._orig_sleep

    def _run(self, coro_fn, name):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with contextlib.suppress(_SleptTwice):
                asyncio.run(self.mod._supervise_loop(coro_fn, name))
        return buf.getvalue()

    def test_disabled_progress_loop_is_not_restarted(self):
        """The shipped default must not spin the supervisor."""
        self.assertFalse(
            self.mod.progress_stream.stream_enabled(),
            "precondition: the flag must be off for this defect to reproduce")

        out = self._run(self.mod.poll_progress, "poll_progress")

        self.assertEqual(
            self.sleeps, 0,
            f"supervisor restarted a deliberately-disabled loop {self.sleeps}x")
        self.assertNotIn("returned unexpectedly", out)

    def test_other_returns_are_still_supervised(self):
        """Control: the fix must not stop supervising genuine early returns."""
        async def _returns_none():
            return None

        out = self._run(_returns_none, "poll_other")

        self.assertGreater(self.sleeps, 0, "a real early return must be restarted")
        self.assertIn("returned unexpectedly", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
