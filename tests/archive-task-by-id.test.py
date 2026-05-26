#!/usr/bin/env python3
"""Behavioral tests for archive_task_by_id() in all three bridges.

Contract:
  1. Bare-name task-X.txt → archived to tasks/archive/YYYY-MM/task-X.txt
  2. Claimed task-X.txt.claimed-core-2.txt → archived
  3. Both present → both archived (no orphan)
  4. Neither present → silent no-op (idempotent, no exception)
  5. Two concurrent calls on same task_id → first wins, second is safe no-op

Run: python3 tests/archive-task-by-id.test.py
Exit: 0 on pass, 1 on fail.
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Minimal stubs for bridge imports
# ---------------------------------------------------------------------------

def _stub_slack_bolt():
    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace()
        def event(self, _name):
            def decorator(fn):
                return fn
            return decorator

    if "slack_bolt" not in sys.modules:
        stub = types.ModuleType("slack_bolt")
        stub.App = _StubApp
        sys.modules["slack_bolt"] = stub
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod
    else:
        import slack_bolt as _real
        _real.App = _StubApp


def _stub_discord():
    discord_mod = sys.modules.get("discord") or types.ModuleType("discord")

    class _Intents:
        @classmethod
        def default(cls):
            obj = cls()
            return obj
        def __setattr__(self, k, v):
            object.__setattr__(self, k, v)

    class _Client:
        def __init__(self, *a, **kw): pass
        async def start(self, *a, **kw): pass
        async def close(self, *a, **kw): pass
        def event(self, fn): return fn
        async def wait_until_ready(self): pass
        def loop(self): return None
        guilds = []
        user = None

    discord_mod.Intents = _Intents
    discord_mod.Client = _Client
    discord_mod.HTTPException = Exception
    discord_mod.Forbidden = Exception
    discord_mod.NotFound = Exception
    discord_mod.File = object

    for name in ["discord", "discord.ext", "discord.ext.commands", "discord.utils"]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["discord"] = discord_mod

    ext_mod = sys.modules["discord.ext"]
    cmds_mod = sys.modules["discord.ext.commands"]
    cmds_mod.Bot = _Client


def _stub_telegram():
    for name in ["telegram", "telegram.ext"]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


def _load_bridge(bridge_name, ws, extra_env=None):
    """Load a bridge module from src/ in isolation."""
    os.environ["SUTANDO_WORKSPACE"] = ws
    if extra_env:
        for k, v in extra_env.items():
            os.environ.setdefault(k, v)

    _stub_slack_bolt()
    _stub_discord()
    _stub_telegram()

    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    bridge_path = repo / "src" / bridge_name
    spec = importlib.util.spec_from_file_location(
        bridge_name.replace("-", "_").replace(".", "_") + "_archive_test",
        bridge_path,
    )
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

def main() -> int:
    passes = 0
    fails = 0

    def expect(name: str, got, want):
        nonlocal passes, fails
        if got == want:
            print(f"PASS: {name}")
            passes += 1
        else:
            print(f"FAIL: {name} — got {got!r}, want {want!r}")
            fails += 1

    bridges = [
        ("slack-bridge.py", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}),
        ("telegram-bridge.py", {"TELEGRAM_BOT_TOKEN": "123:test"}),
        ("discord-bridge.py", {}),
    ]

    for bridge_file, env in bridges:
        ws = tempfile.mkdtemp(prefix=f"sutando-test-{bridge_file[:5]}-")
        try:
            mod = _load_bridge(bridge_file, ws, env)
        except Exception as e:
            print(f"FAIL: could not load {bridge_file}: {e}")
            fails += 1
            continue

        tasks_dir: Path = mod.TASKS_DIR
        tasks_dir.mkdir(parents=True, exist_ok=True)

        label = bridge_file.replace("-bridge.py", "")

        # Case 1: bare-name file → archived
        task_id = "task-111111"
        bare = tasks_dir / f"{task_id}.txt"
        bare.write_text("bare task")
        mod.archive_task_by_id(task_id)
        expect(f"{label}: bare file removed from tasks/", bare.exists(), False)
        # At least one file in archive YYYY-MM
        from datetime import datetime
        ym = datetime.now().strftime("%Y-%m")
        archive_dir: Path = mod.ARCHIVE_TASKS_DIR / ym
        archived = list(archive_dir.glob(f"{task_id}*.txt"))
        expect(f"{label}: bare file archived", len(archived) >= 1, True)

        # Case 2: claimed file → archived
        task_id2 = "task-222222"
        claimed = tasks_dir / f"{task_id2}.txt.claimed-core-2.txt"
        claimed.write_text("claimed task")
        mod.archive_task_by_id(task_id2)
        expect(f"{label}: claimed file removed from tasks/", claimed.exists(), False)
        archived2 = list(archive_dir.glob(f"{task_id2}*.txt"))
        expect(f"{label}: claimed file archived", len(archived2) >= 1, True)

        # Case 3: both bare + claimed present → both archived
        task_id3 = "task-333333"
        bare3 = tasks_dir / f"{task_id3}.txt"
        claimed3 = tasks_dir / f"{task_id3}.txt.claimed-core-1.txt"
        bare3.write_text("bare")
        claimed3.write_text("claimed")
        mod.archive_task_by_id(task_id3)
        expect(f"{label}: both bare+claimed removed", not bare3.exists() and not claimed3.exists(), True)
        archived3 = list(archive_dir.glob(f"{task_id3}*.txt"))
        expect(f"{label}: both files archived", len(archived3) >= 2, True)

        # Case 4: nothing present → silent no-op
        task_id4 = "task-444444"
        try:
            mod.archive_task_by_id(task_id4)
            expect(f"{label}: missing file → no-op (no exception)", True, True)
        except Exception as e:
            expect(f"{label}: missing file → no-op (no exception)", False, True)

        # Case 5: two calls → second is safe (FileNotFoundError swallowed)
        task_id5 = "task-555555"
        dup = tasks_dir / f"{task_id5}.txt"
        dup.write_text("dup")
        mod.archive_task_by_id(task_id5)
        try:
            mod.archive_task_by_id(task_id5)  # second call, file already gone
            expect(f"{label}: second call → safe no-op", True, True)
        except Exception as e:
            expect(f"{label}: second call → safe no-op", False, True)

    print(f"\nResults: {passes} passed, {fails} failed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
