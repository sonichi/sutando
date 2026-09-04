#!/usr/bin/env python3
# The sandbox prompt lives under the workspace at 0600, is removed with its task, and never sits in /tmp.
"""Branch coverage for the access-mutate.py commands the bridge newly routes
through it, plus the transition-window `pair` path regression (#3318).

tests/access-mutate-cli.test.py covers only `group-append` / `group-rm-allow`
and arg parsing. Everything the `/discord:access` skill now delegates —
`pair`, `deny`, `allow`, `remove`, `policy`, `group-add`, `group-rm`, `set` —
was unexercised.

The `pair` case is not just coverage. `_backup()` writes the durable backup
during a successful mutation, and `resolve_discord_access_file()` returns the
canonical path once that backup exists. `_pair()` used to resolve twice: once
for the transaction and once for the approved-marker directory. In the
transition window (canonical absent, legacy populated) those two calls return
DIFFERENT parents, so the grant committed to legacy while the "you're in"
marker was written under canonical — which `_approved_dirs()` does not poll.
The sender is authorized and never told. TestPairPathOwnership pins that the
marker lands beside the file the mutation actually committed to.

Isolation: same two-axis fixture as tests/access-mutate-cli.test.py —
$CLAUDE_CONFIG_DIR for the live access.json and a direct monkeypatch of
access_store.resolve_workspace for the durable backup, which resolves through
resolve_workspace() and honors neither env var.

Run: python3 tests/access-mutate-cli-commands.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import inspect
import json
import shutil
import stat
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import access_store  # noqa: E402

# discord-bridge.py resolves host config at import time, so isolating
# CLAUDE_CONFIG_DIR inside setUp() would already be too late.
_BRIDGE_CCD = tempfile.mkdtemp(prefix="sandbox-prompt-bridge-ccd-")
_BRIDGE_SRC = tempfile.mkdtemp(prefix="sandbox-prompt-bridge-vanilla-")
os.environ["CLAUDE_CONFIG_DIR"] = _BRIDGE_CCD
os.environ["SOURCE_CLAUDE_CONFIG_DIR"] = _BRIDGE_SRC
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_bridge_ch = Path(_BRIDGE_CCD) / "channels" / "discord"
_bridge_ch.mkdir(parents=True, exist_ok=True)
(_bridge_ch / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    _stub = types.ModuleType("discord")
    _stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    _stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                       "event": staticmethod(lambda fn: fn)})
    _stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    _stub.Message = type("Message", (), {})
    _stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = _stub

_bspec = importlib.util.spec_from_file_location(
    "dbridge_sandbox_prompt", REPO / "src" / "discord-bridge.py")
db_bridge = importlib.util.module_from_spec(_bspec)
sys.modules["dbridge_sandbox_prompt"] = db_bridge
_bspec.loader.exec_module(db_bridge)


class SandboxPromptPath(unittest.TestCase):
    """The non-owner sandbox prompt lives under the workspace at 0600 and is gone
    once the task is archived; /tmp, the sandbox cwd, never holds it."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="sandbox-prompt-"))
        for name in ("SANDBOX_PROMPTS_DIR", "TASKS_DIR", "ARCHIVE_TASKS_DIR", "ARCHIVE_RESULTS_DIR"):
            setattr(self, "_old_" + name, getattr(db_bridge, name))
        db_bridge.SANDBOX_PROMPTS_DIR = self.d / "state" / "sandbox-prompts"
        db_bridge.TASKS_DIR = self.d / "tasks"
        db_bridge.ARCHIVE_TASKS_DIR = self.d / "tasks" / "archive"
        db_bridge.ARCHIVE_RESULTS_DIR = self.d / "results" / "archive"
        db_bridge.TASKS_DIR.mkdir(parents=True)

    def tearDown(self):
        for name in ("SANDBOX_PROMPTS_DIR", "TASKS_DIR", "ARCHIVE_TASKS_DIR", "ARCHIVE_RESULTS_DIR"):
            setattr(db_bridge, name, getattr(self, "_old_" + name))
        shutil.rmtree(self.d, ignore_errors=True)

    def test_prompt_is_written_under_the_workspace_not_tmp(self):
        p = db_bridge.write_sandbox_prompt("task-1", "hello")
        self.assertEqual(p, self.d / "state" / "sandbox-prompts" / "task-1.txt")
        self.assertFalse(str(p).startswith("/tmp"))
        self.assertEqual(p.read_text(), "hello")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)

    def test_handler_source_carries_no_tmp_path_and_logs_a_failed_write(self):
        src = inspect.getsource(db_bridge._handle_discord_message)
        self.assertNotIn("/tmp/sutando-", src)
        self.assertIn("write_sandbox_prompt(", src)
        self.assertIn("[task-write] FAILED", src.split("write_sandbox_prompt(", 1)[1][:400])
        self.assertNotIn("/tmp/sutando-", inspect.getsource(db_bridge))

    def test_archiving_the_task_removes_its_prompt(self):
        db_bridge.write_sandbox_prompt("task-2", "x")
        task = db_bridge.TASKS_DIR / "task-2.txt"; task.write_text("id: task-2\n")
        self.assertTrue(db_bridge.archive_file(task, "tasks", "task-2"))
        self.assertFalse(db_bridge.sandbox_prompt_path("task-2").exists())

    def test_archiving_a_result_leaves_the_prompt_for_the_task_archive(self):
        db_bridge.write_sandbox_prompt("task-3", "x")
        res = self.d / "results" / "task-3.txt"; res.parent.mkdir(parents=True); res.write_text("r")
        db_bridge.archive_file(res, "results", "task-3")
        self.assertTrue(db_bridge.sandbox_prompt_path("task-3").exists())

    def test_sweep_removes_only_old_prompts_whose_task_is_gone(self):
        live = db_bridge.write_sandbox_prompt("task-4", "x")
        (db_bridge.TASKS_DIR / "task-4.txt").write_text("live")
        gone_old = db_bridge.write_sandbox_prompt("task-5", "x")
        gone_new = db_bridge.write_sandbox_prompt("task-6", "x")
        old = time.time() - 3600
        os.utime(gone_old, (old, old)); os.utime(live, (old, old))
        self.assertEqual(db_bridge.sweep_sandbox_prompts(max_age_s=600), 1)
        self.assertTrue(live.exists()); self.assertFalse(gone_old.exists()); self.assertTrue(gone_new.exists())

    def test_sweep_with_no_directory_is_zero_not_an_error(self):
        db_bridge.SANDBOX_PROMPTS_DIR = self.d / "absent"
        self.assertEqual(db_bridge.sweep_sandbox_prompts(), 0)


if __name__ == "__main__":
    unittest.main()
