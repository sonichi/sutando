#!/usr/bin/env python3
"""Each bridge's `_dedup_recover` routes the shared plan and swallows nothing.

The wrappers are thin by design; what matters is that they bind their own
directories, return what the caller must route, and never let a recovery
failure take down the delivery loop.
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Bridges construct clients at import; stub what is not installed and redirect
# HOME so importing never touches a real credential path.
try:
    import discord  # noqa: F401
except ImportError:
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(lambda: type("X", (), {"message_content": False})())})
    _d.Client = type("C", (), {"__init__": lambda self, **k: None, "event": staticmethod(lambda fn: fn)})
    _d.File = type("F", (), {})
    _d.Message = type("M", (), {})
    sys.modules["discord"] = _d

# Bridges resolve channel access at import and fall back to the real
# ~/.claude, so CLAUDE_CONFIG_DIR must be isolated first.

# Stub slack_bolt regardless: the real App() runs a live auth.test at
# construction, so importing the bridge would hit the network.
_sb = types.ModuleType("slack_bolt")
_sb.App = type("App", (), {"__init__": lambda self, **kw: None,
                           "event": lambda self, name: (lambda fn: fn),
                           "client": None})
sys.modules["slack_bolt"] = _sb
sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
_sm = types.ModuleType("slack_bolt.adapter.socket_mode")
_sm.SocketModeHandler = type("SocketModeHandler", (),
                             {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter.socket_mode"] = _sm

_CFG = tempfile.mkdtemp(prefix="ccd-dedup-wrappers-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")

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

TID = "task-633325612fbde6e777"
HOLDER = "task-22d83e59601f3a1fef"
ORIG = f"id: {TID}\nsource: x\naccess_tier: owner\ntask: What is AG2Space?\n"

BRIDGES = {
    "discord": "discord-bridge.py",
    "slack": "slack-bridge.py",
    "telegram": "telegram-bridge.py",
}


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(f"_dedup_{name}", REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class BridgeWrapperTest(unittest.TestCase):
    def _seed(self, mod, td: str, holder_body: str | None, orig: str | None = ORIG):
        results, tasks = Path(td) / "results", Path(td) / "tasks"
        (results / "archive").mkdir(parents=True)
        tasks.mkdir(parents=True)
        if holder_body is not None:
            (results / "archive" / f"{HOLDER}-1785976425.txt").write_text(holder_body)
        if orig is not None:
            (tasks / f"{TID}.txt").write_text(orig)
        mod.RESULTS_DIR, mod.TASKS_DIR = results, tasks
        return results, tasks

    def _each(self):
        """Yield every importable bridge. A bridge whose SDK is absent is skipped
        individually; skipping one must not hide the others."""
        loaded = 0
        for name, filename in BRIDGES.items():
            try:
                mod = _load(name, filename)
            except (Exception, SystemExit):  # noqa: BLE001 - optional SDK absent
                continue
            loaded += 1
            yield name, mod
        if loaded == 0:
            self.skipTest("no bridge importable in this environment")

    def _call(self, name, mod, chan="C1"):
        """Normalise the three signatures, with every send stubbed.

        The report branch notifies the asker; unstubbed that is a real HTTP
        call to the provider from a unit test.
        """
        sent = []
        stubs = {"send_reply": lambda *a, **k: sent.append(a) or {"ok": True},
                 "_send_reply": lambda *a, **k: sent.append(a) or {"ok": True}}
        saved = {n: getattr(mod, n) for n in stubs if hasattr(mod, n)}
        for n, fn in stubs.items():
            if hasattr(mod, n):
                setattr(mod, n, fn)
        try:
            if name == "slack":
                return mod._dedup_recover(TID, HOLDER, {"channel": chan})
            return mod._dedup_recover(TID, HOLDER, chan)
        finally:
            for n, fn in saved.items():
                setattr(mod, n, fn)

    def test_wrapper_binds_its_own_dirs_and_requeues(self):
        for name, mod in self._each():
            with self.subTest(bridge=name):
                saved = (mod.RESULTS_DIR, mod.TASKS_DIR)
                try:
                    with tempfile.TemporaryDirectory() as td:
                        _, tasks = self._seed(mod, td, "")
                        self._call(name, mod)
                        written = [p for p in tasks.glob("task-*.txt") if p.stem != TID]
                        self.assertEqual(
                            len(written), 1,
                            f"{name}: no re-ask written — the wrapper is not bound to "
                            f"its own TASKS_DIR, or it swallowed the plan")
                        self.assertIn("delivered nothing", written[0].read_text())
                finally:
                    mod.RESULTS_DIR, mod.TASKS_DIR = saved

    def test_wrapper_honours_a_holder_that_answered(self):
        for name, mod in self._each():
            with self.subTest(bridge=name):
                saved = (mod.RESULTS_DIR, mod.TASKS_DIR)
                try:
                    with tempfile.TemporaryDirectory() as td:
                        _, tasks = self._seed(mod, td, "a real answer")
                        self._call(name, mod)
                        self.assertEqual(
                            [p for p in tasks.glob("task-*.txt") if p.stem != TID], [],
                            f"{name}: re-asked a dedup whose holder answered")
                finally:
                    mod.RESULTS_DIR, mod.TASKS_DIR = saved

    def test_wrapper_never_raises_into_the_delivery_loop(self):
        """A recovery failure must not take the poll loop down with it."""
        for name, mod in self._each():
            with self.subTest(bridge=name):
                saved = (mod.RESULTS_DIR, mod.TASKS_DIR)
                try:
                    mod.RESULTS_DIR = mod.TASKS_DIR = Path("/nonexistent/dedup-test")
                    self._call(name, mod)  # must not raise
                finally:
                    mod.RESULTS_DIR, mod.TASKS_DIR = saved

    def test_wrapper_swallows_a_raising_plan(self):
        """Directories that merely do not exist are handled inside the plan;
        this drives the wrapper's own guard by making the plan itself raise."""
        for name, mod in self._each():
            with self.subTest(bridge=name):
                original = mod.plan_dedup_recovery

                def _boom(*a, **k):
                    raise RuntimeError("plan exploded")

                mod.plan_dedup_recovery = _boom
                try:
                    self._call(name, mod)  # must not propagate
                finally:
                    mod.plan_dedup_recovery = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
