#!/usr/bin/env python3
"""Tests for discord-bridge's owner easy-restart intercept (sonichi#2401,
PR #2408): `_handle_restart_command` — the bridge-side seam of the
chat-triggered restart. Hermetic real-module import (discord SDK stubbed,
temp workspace via SUTANDO_TEST_MODE) per the bridge-audit-wiring pattern,
so the production lines execute under the diff-coverage gate.

Covers: owner command → intent written + ack sent + True (handled);
non-command prose → False; team tier → False (never writes); write failure
→ error ack, still handled; ack-send failure swallowed (bridge never
crashes on a Discord hiccup).

Run: python3 tests/bridge-restart-intercept.test.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WS = tempfile.mkdtemp()
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# Stub `discord` so module load needs no SDK/network (bridge-audit-wiring recipe).
try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                      "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

spec = importlib.util.spec_from_file_location("dbridge_restart", REPO / "src" / "discord-bridge.py")
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


class _Chan:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text):
        if self.fail:
            raise RuntimeError("discord hiccup")
        self.sent.append(text)


class _Msg:
    def __init__(self, chan):
        self.channel = chan


def _run(text, tier="owner", ws=None, chan=None):
    chan = chan if chan is not None else _Chan()
    handled = asyncio.run(
        db._handle_restart_command(_Msg(chan), text, tier, "tester", ws or tempfile.mkdtemp()))
    return handled, chan


# --- owner restart command: intent written, ack sent, handled ---
ws = tempfile.mkdtemp()
handled, chan = _run("restart core", ws=ws)
intent_file = os.path.join(ws, "state", "core-restart-requested.json")
check("owner 'restart core' → handled=True", handled is True)
check("intent file written with action=restart",
      os.path.exists(intent_file) and json.load(open(intent_file))["action"] == "restart",
      intent_file)
check("ack sent mentions relaunch", len(chan.sent) == 1 and "Restart requested" in chan.sent[0])

# --- owner stop command ---
ws2 = tempfile.mkdtemp()
handled, chan = _run("Stop core.", ws=ws2)
check("owner 'Stop core.' → handled + action=stop",
      handled and json.load(open(os.path.join(ws2, "state", "core-restart-requested.json")))["action"] == "stop")
check("stop ack says stays stopped", "stays stopped" in chan.sent[0])

# --- prose and non-owner never trigger ---
handled, chan = _run("we should restart core tomorrow")
check("prose → handled=False, nothing sent", handled is False and chan.sent == [])
ws3 = tempfile.mkdtemp()
handled, chan = _run("restart core", tier="team", ws=ws3)
check("team tier → handled=False, no intent written",
      handled is False and not os.path.exists(os.path.join(ws3, "state", "core-restart-requested.json")))
handled, chan = _run("", ws=tempfile.mkdtemp())
check("empty text → handled=False", handled is False)

# --- write failure → error ack, still handled (no task-file fallthrough) ---
ro = tempfile.mkdtemp()
blocker = os.path.join(ro, "state")
open(blocker, "w").close()  # 'state' exists as a FILE → makedirs raises
handled, chan = _run("restart core", ws=ro)
check("write failure → still handled, error ack",
      handled is True and len(chan.sent) == 1 and "Couldn't write" in chan.sent[0], str(chan.sent))

# --- ack-send failure swallowed ---
ws4 = tempfile.mkdtemp()
handled, chan = _run("restart core", ws=ws4, chan=_Chan(fail=True))
check("ack send failure swallowed, still handled + intent written",
      handled is True and os.path.exists(os.path.join(ws4, "state", "core-restart-requested.json")))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — bridge restart intercept")
