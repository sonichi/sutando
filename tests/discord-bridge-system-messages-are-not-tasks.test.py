#!/usr/bin/env python3
"""A Discord-authored system message must not enter the task pipeline.

Creating a thread posts a THREAD_CREATED notice in the parent channel whose
`content` is the thread NAME and whose body is empty. The bridge logged the
non-default type and kept going, so that notice became a task file whose whole
body was a bare title. Observed 2026-08-15 as task-1786810290354.

`Message.is_system()` is the library's own answer to "does this carry user
content" — it deliberately keeps `thread_starter_message` and the slash-command
types, which do. Enumerating types here instead would drift from that list.
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

# Isolate BEFORE importing the bridge: it resolves channel config at module scope.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-sysmsg-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n", encoding="utf-8")
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")

sys.path.insert(0, str(REPO / "src"))

try:
    import discord  # noqa: F401
    HAVE_DISCORD = True
except ImportError:  # pragma: no cover - depends on the runner's site-packages
    HAVE_DISCORD = False
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(
        lambda: type("X", (), {"message_content": False, "members": False})())})
    _d.Client = type("C", (), {
        "__init__": lambda self, **k: setattr(self, "user", None),
        "event": staticmethod(lambda fn: fn)})
    _d.File = type("F", (), {})
    _d.Message = type("M", (), {})
    _d.DMChannel = type("DM", (), {})
    _d.AllowedMentions = type("AM", (), {"__init__": lambda self, **k: None})
    _d.MessageType = types.SimpleNamespace(default=0, reply=19, thread_created=18)
    sys.modules["discord"] = _d


def load_bridge():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(compile(src, bridge.__file__, "exec"), bridge.__dict__)
    return bridge


bridge = load_bridge()

pass_n = 0
fail_n = 0


def check(ok, label):
    global pass_n, fail_n
    if ok:
        print(f"  ok  {label}")
        pass_n += 1
    else:
        print(f"  FAIL {label}")
        fail_n += 1


def _guild_message(system: bool, mtype=0, content="PR practice — runs, reflections, changes"):
    """A channel message shaped like the real THREAD_CREATED notice."""
    return types.SimpleNamespace(
        id=1538218588937134170,
        type=mtype,
        content=content,
        clean_content=content,
        attachments=[],
        embeds=[],
        mentions=[],
        reference=None,
        guild=types.SimpleNamespace(id=1153072414184452236, name="Sutando-private"),
        channel=types.SimpleNamespace(id=1490415515502383137, name="bot2bot"),
        author=types.SimpleNamespace(id=1490412828065267872, bot=True,
                                     name="Sutando-Mini", display_name="Sutando-Mini"),
        is_system=lambda: system,
    )


def _run(msg):
    """Drive on_message with the first post-guard call recorded."""
    seen = []
    orig = bridge._load_welcome_config
    bridge._load_welcome_config = lambda gid: (seen.append(gid) or (None, None))
    try:
        asyncio.run(bridge.on_message(msg))
    except Exception as exc:                      # a later gate may raise on a stub
        seen.append(("raised", type(exc).__name__))
    finally:
        bridge._load_welcome_config = orig
    return seen


# POSITIVE CONTROL FIRST. Without it, "nothing downstream ran" is satisfied by a
# harness that never reaches the guard at all.
reached = _run(_guild_message(system=False))
check(bool(reached), "CONTROL: a non-system message reaches the code after the guard")

blocked = _run(_guild_message(system=True))
check(not blocked, "a system message stops at the guard")

# The real shape, against the library's own enum rather than a hand-written int.
if HAVE_DISCORD:
    import discord as _real
    real_notice = _guild_message(system=True, mtype=_real.MessageType.thread_created)
    check(not _run(real_notice), "a real THREAD_CREATED notice stops at the guard")

    # And the library must still treat the types that DO carry content as ours.
    check(not _real.Message.is_system(types.SimpleNamespace(
        type=_real.MessageType.thread_starter_message)),
        "PREMISE: thread_starter_message is NOT a system message (it carries the post)")
    check(_real.Message.is_system(types.SimpleNamespace(
        type=_real.MessageType.thread_created)),
        "PREMISE: thread_created IS a system message")
else:                                             # pragma: no cover
    print("  skip real-enum cases (discord.py not installed)")

print()
if fail_n == 0:
    print(f"PASS — {pass_n} checks green")
else:
    print(f"FAIL — {fail_n} failed, {pass_n} passed")
sys.exit(1 if fail_n else 0)
