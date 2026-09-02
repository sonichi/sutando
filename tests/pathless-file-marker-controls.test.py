#!/usr/bin/env python3
"""BEHAVIOURAL: a `[file:]` with no path is SENT to the channel, on both bridges.

The sibling suite asserts the branch exists in source. This one drives the real
delivery paths, because a branch that is present and never reached is the defect
this whole PR is about.

Both sinks previously routed `attach("")` into the log-only "prose quotation"
branch and then marked the result delivered, so a file-only result produced zero
user-visible output and was durably retired.

HERMETIC: `CLAUDE_CONFIG_DIR` is a tmpdir seeded before either module imports,
`discord` is stubbed, and every network call is replaced by a recorder. The
Discord loop is `while True: ... ; await asyncio.sleep(N)` with the sleep outside
the per-task try, so patching sleep to raise gives exactly one iteration — the
same driver `proactive-dm-failure-keeps-file-behaviour` uses.

  a) telegram send_reply("[file:]") issues a sendMessage saying so
  b) discord poll_results() on a `[file:]` result sends to the channel
  c) a REAL absent path still takes the quiet branch on both (not widened)

Run: python3 tests/pathless-file-marker-controls.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-pathless-")
_cfg_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": ["4242"]}')
_cfg_telegram = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg_telegram.mkdir(parents=True, exist_ok=True)
(_cfg_telegram / "access.json").write_text('{"allowFrom": ["4242"]}')
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ["SUTANDO_TEST_MODE"] = "1"
sys.path.insert(0, str(REPO / "src"))

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
    _stub.MessageReference = type("MessageReference", (), {
        "__init__": lambda self, **kw: None})
    _stub.HTTPException = type("HTTPException", (Exception,), {})
    sys.modules["discord"] = _stub

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


NEEDLE = "no path"


def telegram_case(body: str) -> list:
    tg = _load("tgb_pathless", REPO / "src" / "telegram-bridge.py")
    sent: list = []
    tg.api = lambda method, **kw: (sent.append((method, kw.get("text", ""))) or {"ok": True})
    tg.send_file = lambda *a, **k: {"ok": True}
    tg.send_reply(4242, body)
    return sent


class _Stop(Exception):
    """Ends poll_results after exactly one iteration."""


def discord_case(body: str) -> list:
    db = _load("dbridge_pathless", REPO / "src" / "discord-bridge.py")
    root = Path(tempfile.mkdtemp(prefix="dc-pathless-"))
    (root / "tasks").mkdir()
    (root / "results").mkdir()
    db.TASKS_DIR = root / "tasks"
    db.RESULTS_DIR = root / "results"
    db.STATE_DIR = root / "state"
    db.STATE_DIR.mkdir(exist_ok=True)
    tid = "task-pathless-1"
    # OWNER tier, or the Team guard withholds the marker before the sink runs.
    (db.TASKS_DIR / f"{tid}.txt").write_text(
        f"id: {tid}\naccess_tier: owner\ntask: fixture\n", encoding="utf-8")
    (db.RESULTS_DIR / f"{tid}.txt").write_text(body, encoding="utf-8")

    sent: list = []

    class _Ch:
        id = 4242
        name = "game"

        async def send(self, content=None, **kw):
            sent.append(content)
            return types.SimpleNamespace(id=1)

    db.pending_replies = {tid: _Ch()}
    db.save_pending_replies = lambda *a, **k: None

    async def _sleep(_n):
        raise _Stop
    orig = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(db.poll_results())
    except _Stop:
        pass
    except Exception as exc:  # surface, don't swallow — a harness fault is a finding
        print(f"  (poll_results raised {type(exc).__name__}: {exc})")
    finally:
        db.asyncio.sleep = orig
    return sent


def main() -> int:
    # a) telegram
    tg_sent = telegram_case("[file:]")
    check(any(NEEDLE in str(t) for _m, t in tg_sent),
          f"a) telegram SENDS the pathless-marker notice, got {[t for _m, t in tg_sent]}")

    # c-telegram) an absent but real-looking path stays quiet
    tg_quiet = telegram_case("[file: /tmp/no-such-9c3f/report.pdf]")
    check(not any(NEEDLE in str(t) for _m, t in tg_quiet),
          f"c) telegram stays quiet for a real-looking absent path, got {[t for _m, t in tg_quiet]}")

    # b) discord
    dc_sent = discord_case("[file:]")
    check(any(NEEDLE in str(c) for c in dc_sent),
          f"b) discord SENDS the pathless-marker notice, got {dc_sent}")

    # c-discord) same control
    dc_quiet = discord_case("[file: /tmp/no-such-9c3f/report.pdf]")
    check(not any(NEEDLE in str(c) for c in dc_quiet),
          f"c) discord stays quiet for a real-looking absent path, got {dc_quiet}")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:3]))
        return 1
    print("PASS — both sinks speak up for a pathless file marker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
