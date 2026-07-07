#!/usr/bin/env python3
"""Discord + Telegram delivery paths write the §7 audit line — Result Router S5.

Wires the shared `result_audit` sink into each bridge's single testable
delivery choke point:
  - Discord: `_mark_delivered(task_id)` — the one post-successful-send hook.
  - Telegram: `send_reply(..., task_id=...)` — records delivered/failed; a
    proactive send (no task_id) is NOT audited.

Both bridges' module load has side effects (SDK import, token read) that fail in
clean CI, so we stub them and run against a hermetic temp workspace.

Run: python3 tests/bridge-audit-wiring.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WS = tempfile.mkdtemp()
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ["SUTANDO_TEST_MODE"] = "1"
AUDIT = Path(_WS) / "state" / "result-audit.log"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Discord: stub `discord` + materialize a fake bot .env, then test _mark_delivered ──
try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

_dc_env = Path.home() / ".claude" / "channels" / "discord" / ".env"
if not _dc_env.exists():
    _dc_env.parent.mkdir(parents=True, exist_ok=True)
    _dc_env.write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

db = _load("dbridge_audit", REPO / "src" / "discord-bridge.py")
db._mark_delivered("task-disc-1")
check("discord: _mark_delivered writes a delivered audit line",
      AUDIT.exists() and "\ttask-disc-1\tdelivered\tdiscord" in AUDIT.read_text(),
      AUDIT.read_text() if AUDIT.exists() else "(no audit file)")

# ── Telegram: set token, stub the network `api`, test send_reply audit ──
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
tg = _load("tgbridge_audit", REPO / "src" / "telegram-bridge.py")

# Force success/failure deterministically by replacing the network call.
tg.api = lambda *a, **k: {"ok": True}
tg.send_reply(12345, "a short reply", task_id="task-tel-1")
_atext = AUDIT.read_text()
check("telegram: send_reply(task_id) records 'delivered'",
      "\ttask-tel-1\tdelivered\ttelegram" in _atext, _atext)

tg.api = lambda *a, **k: {"ok": False}  # simulate a send failure
tg.send_reply(12345, "another reply", task_id="task-tel-2")
_atext = AUDIT.read_text()
check("telegram: failed send records 'failed'",
      "\ttask-tel-2\tfailed\ttelegram" in _atext)

# Proactive send (no task_id) must NOT be audited.
tg.api = lambda *a, **k: {"ok": True}
_before = AUDIT.read_text()
tg.send_reply(12345, "proactive ping, not a result")  # no task_id
_after = AUDIT.read_text()
check("telegram: proactive send (no task_id) is not audited", _before == _after)

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — discord + telegram audit-wiring tests")
