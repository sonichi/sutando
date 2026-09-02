#!/usr/bin/env python3
"""Drive the REAL bridge polling loops through the destination gates.

The direct predicate checks in proactive-destination.test.py prove the
policy; these passes prove the WIRING — one real iteration of discord's
poll_dm_fallback and slack's result_watcher, with a spy recording what the
gate saw and decided for each seeded file.

Single-pass technique per proactive-dm-failure-keeps-file-behaviour: the
sleep at the end of each loop is patched to raise a sentinel.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Hermetic (module level, before any bridge import): bridges resolve channel
# config at import — point them at a seeded temp dir, never the real one.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="dest-drive-ccd-")
_DISCORD_CFG = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_DISCORD_CFG.mkdir(parents=True, exist_ok=True)
(_DISCORD_CFG / "access.json").write_text('{"allowFrom": ["4242"]}')
_SLACK_CFG = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_SLACK_CFG.mkdir(parents=True, exist_ok=True)
(_SLACK_CFG / "access.json").write_text('{"allowFrom": ["4242"]}')
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Sentinel(Exception):
    """Breaks the poll loop after exactly one pass."""


def drive_discord_fallback():
    try:
        import discord  # noqa: F401
    except Exception:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(
            lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                          "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
        stub.Message = type("Message", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        sys.modules["discord"] = stub

    db = _load("dbridge_dest_drive", REPO / "src" / "discord-bridge.py")
    td = Path(tempfile.mkdtemp(prefix="dest-drive-results-"))
    for name in ("briefing-1.to-telegram.txt", "briefing-2.to-discord.txt",
                 "briefing-3.txt"):
        (td / name).write_text("body")  # fresh: inside the grace window
    db.RESULTS_DIR = td

    import proactive_routing as pr
    seen = {}
    real = pr.fallback_claims_name
    pr.fallback_claims_name = lambda n, ch: seen.setdefault(n, real(n, ch))

    async def _sleep(_secs):
        raise _Sentinel()

    orig_sleep = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(db.poll_dm_fallback())
        check("discord fallback: loop pass completed", False)
    except _Sentinel:
        check("discord fallback: loop pass completed", True)
    finally:
        db.asyncio.sleep = orig_sleep
        pr.fallback_claims_name = real

    check("discord fallback: gate consulted for every prefix-matched file",
          set(seen) == {"briefing-1.to-telegram.txt",
                        "briefing-2.to-discord.txt", "briefing-3.txt"},
          str(seen))
    check("discord fallback: foreign tag refused in-flow",
          seen.get("briefing-1.to-telegram.txt") is False)
    check("discord fallback: own tag and undestined pass the gate in-flow",
          seen.get("briefing-2.to-discord.txt") is True
          and seen.get("briefing-3.txt") is True)
    check("discord fallback: foreign-tagged file untouched",
          (td / "briefing-1.to-telegram.txt").exists())


def drive_slack_watcher():
    for _m in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
        if _m in sys.modules:
            continue
        mod = types.ModuleType(_m)
        if _m == "slack_bolt":
            mod.App = type("App", (), {"__init__": lambda self, **kw: None,
                                       "event": lambda self, *a, **kw: (lambda fn: fn),
                                       "client": None})
        if _m.endswith("socket_mode"):
            mod.SocketModeHandler = type("SocketModeHandler", (),
                                         {"__init__": lambda self, *a, **kw: None})
        sys.modules[_m] = mod

    sb = _load("sbridge_dest_drive", REPO / "src" / "slack-bridge.py")
    td = Path(tempfile.mkdtemp(prefix="dest-drive-slack-"))
    (td / "proactive-9.to-discord.txt").write_text("plain body")
    sb.RESULTS_DIR = td
    sb.TASKS_DIR = Path(tempfile.mkdtemp(prefix="dest-drive-slack-tasks-"))
    sb.STATE_DIR = Path(tempfile.mkdtemp(prefix="dest-drive-slack-state-"))
    sb.presenter_mode_active = lambda *_a, **_k: False

    # Spy on the SHARED owner symbol, not the adapter wrapper: this pins that
    # the watcher's decision actually delegates to proactive_routing.
    seen = {}
    real = sb.fallback_claims_name
    sb.fallback_claims_name = lambda n, ch: seen.setdefault((n, ch), real(n, ch))

    def _sleep(_secs):
        raise _Sentinel()

    orig_sleep = sb.time.sleep
    sb.time.sleep = _sleep
    try:
        sb.result_watcher()
        check("slack watcher: loop pass completed", False)
    except _Sentinel:
        check("slack watcher: loop pass completed", True)
    finally:
        sb.time.sleep = orig_sleep
        sb.fallback_claims_name = real

    check("slack watcher: shared routing owner consulted in-flow",
          seen.get(("proactive-9.to-discord.txt", "slack")) is False, str(seen))
    check("slack watcher: foreign-destined file left unclaimed",
          (td / "proactive-9.to-discord.txt").exists())


def main() -> int:
    drive_discord_fallback()
    drive_slack_watcher()
    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}", file=sys.stderr)
        return 1
    print("\nPASS: destination gates exercised through the real bridge loops")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
