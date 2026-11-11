#!/usr/bin/env python3
"""Each bridge's startup hands its resolved TASKS_DIR to the shared inbox reporter.

Drives the real startup entry point of slack, telegram and discord with the SDKs
stubbed; the entry point is stopped at the report, so no connection is made.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))  # `python3 -I` does not add the script dir

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="ws-bridge-inbox-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-bridge-inbox-adapters-")
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text('{"allowFrom": []}')
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text('{"allowFrom": []}')
_telegram_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_telegram_cfg.mkdir(parents=True, exist_ok=True)
(_telegram_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-placeholder-token"
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"
sys.path.insert(0, str(ROOT / "src"))


class _FakeApp:
    def __init__(self, token=None):
        self.client = types.SimpleNamespace()

    def _decorator(self, *a, **k):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


def _stub_sdks() -> None:
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        bolt = types.ModuleType("slack_bolt")
        bolt.App = _FakeApp
        sock = types.ModuleType("slack_bolt.adapter.socket_mode")
        sock.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
        sys.modules.update({"slack_bolt": bolt,
                            "slack_bolt.adapter": types.ModuleType("slack_bolt.adapter"),
                            "slack_bolt.adapter.socket_mode": sock})
    try:
        import discord  # noqa: F401
    except ImportError:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(
            lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                          "event": staticmethod(lambda fn: fn)})
        for name in ("File", "Message", "DMChannel"):
            setattr(stub, name, type(name, (), {}))
        sys.modules["discord"] = stub


class _Stop(Exception):
    pass


class _Missed(Exception):
    """Startup went past the report without making it."""


def _load(bridge: str):
    spec = importlib.util.spec_from_file_location(bridge.replace("-", "_"), ROOT / "src" / f"{bridge}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AdaptersReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _stub_sdks()

    def _drive(self, bridge: str, entry: str, next_step: str):
        try:
            mod = _load(bridge)
        except Exception as exc:  # noqa: BLE001 — an SDK the stub cannot satisfy
            self.skipTest(f"{bridge} could not load here: {type(exc).__name__}: {exc}")
        calls = []

        def record(name, tasks_dir, *a, **k):
            calls.append((name, tasks_dir))
            raise _Stop

        mod._single_instance_acquire = lambda *a, **k: None
        mod.report_inbox = record

        def missed(*a, **k):
            raise _Missed
        setattr(mod, next_step, missed)
        with self.assertRaises(_Stop):
            getattr(mod, entry)()
        self.assertEqual(calls, [(bridge, mod.TASKS_DIR)])
        self.assertEqual(mod.TASKS_DIR, mod.resolve_workspace() / "tasks")

    def test_slack(self):
        self._drive("slack-bridge", "main", "_recover_orphan_sending_files")

    def test_telegram(self):
        self._drive("telegram-bridge", "main", "log_privacy_setting")

    def test_discord(self):
        self._drive("discord-bridge", "_run_bridge", "_log_gateway_reasons")


if __name__ == "__main__":
    unittest.main()
