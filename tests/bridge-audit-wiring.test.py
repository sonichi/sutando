#!/usr/bin/env python3
"""Discord delivery path writes the §7 audit line — Result Router S5.

Wires the shared `result_audit` sink into Discord's single testable delivery
choke point: `_mark_delivered(task_id)`, the one hook that fires after a
successful `channel.send` (text + any files) in `poll_results` → records
`delivered`. Because the file-send loop runs *before* `_mark_delivered` inside
the same try, a failed attachment hits the `except` and `_mark_delivered` never
runs — so the audit reflects the FULL delivery, never a premature `delivered`.

(Telegram's audit was deferred: its `send_reply` doesn't send the caller's
`parsed.actions` attachments, so recording there would miss attachment failures.
It lands correctly at the delivery-outcome layer in the poller-extraction
follow-up. Slack landed in #1984, where `_send_reply` sends its own files.)

discord-bridge's module load has side effects (discord SDK import, token read)
that fail in clean CI, so we stub them and run against a hermetic temp workspace.

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


# Stub `discord` + materialize a fake bot .env so module load works in CI.
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

# A second delivery appends (one line per delivered result).
db._mark_delivered("task-disc-2")
lines = [l for l in AUDIT.read_text().splitlines() if "\tdiscord" in l]
check("discord: appends one audit line per delivered result", len(lines) == 2, str(lines))
check("discord: every line has the delivered disposition + discord surface",
      all(l.split("\t")[2:] == ["delivered", "discord"] for l in lines))

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — discord audit-wiring tests")
