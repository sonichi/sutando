#!/usr/bin/env python3
"""A Discord-authored system message must not reach ANY content consumer — the
guard sits ahead of the mod observer, so a bare thread title is never judged."""
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

checkpoints = []
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


class _FakeDM:
    """isinstance target for the DM branch: the real DMChannel cannot be built here."""
    def __init__(self, cid):
        self.id = cid
        self.name = "dm"


def _dm_message(system: bool, mtype=0):
    m = _guild_message(system=system, mtype=mtype)
    m.guild = None
    m.channel = _FakeDM(999000111222333444)
    return m


def _run(msg, checkpoint_raises=False):
    """Drive on_message; returns (observed, reached).
    `observed` is the moderation hook the guard used to sit BEHIND."""
    observed, reached = [], []
    checkpoints.clear()

    async def _spy_observe(m):
        observed.append(getattr(m, "id", None))

    orig_obs, orig_wel = bridge._observe_for_mod, bridge._load_welcome_config
    orig_ck, orig_dm = bridge._update_dm_checkpoint, bridge.discord.DMChannel
    bridge._observe_for_mod = _spy_observe
    bridge._load_welcome_config = lambda gid: (reached.append(gid) or (None, None))
    def _spy_checkpoint(cid, mid):
        checkpoints.append((cid, mid))
        if checkpoint_raises:
            raise OSError("checkpoint store unwritable")

    bridge._update_dm_checkpoint = _spy_checkpoint
    bridge.discord.DMChannel = _FakeDM
    try:
        asyncio.run(bridge.on_message(msg))
    except Exception as exc:                      # a later gate may raise on a stub
        reached.append(("raised", type(exc).__name__))
    finally:
        bridge._observe_for_mod, bridge._load_welcome_config = orig_obs, orig_wel
        bridge._update_dm_checkpoint, bridge.discord.DMChannel = orig_ck, orig_dm
    return observed, reached


# POSITIVE CONTROLS FIRST. Without them, "nothing downstream ran" is satisfied by a
# harness that never reaches the guard at all.
obs, reached = _run(_guild_message(system=False))
check(bool(reached), "CONTROL: a non-system message reaches the code after the guard")
check(bool(obs), "CONTROL: a non-system message DOES reach the moderation observer")

obs, blocked = _run(_guild_message(system=True))
check(not blocked, "a system message stops at the guard")
check(not obs, "a system message never reaches the moderation observer")

# A system DM must ALSO advance the checkpoint: returning without it re-creates
# the frozen-checkpoint starvation path the self-message branch documents.
obs, reached = _run(_dm_message(system=True))
check(not reached and not obs, "a system DM stops at the guard")
check(len(checkpoints) == 1, f"a system DM still advances the checkpoint (got {len(checkpoints)})")

obs, reached = _run(_dm_message(system=False))
check(not checkpoints or checkpoints[-1][0] == 999000111222333444,
      "CONTROL: the checkpoint spy is wired to the same channel id")

# A failing checkpoint store must not break the drop: the guard exists to keep the
# notice out of the pipeline, and that must not depend on a write succeeding.
obs, reached = _run(_dm_message(system=True), checkpoint_raises=True)
check(len(checkpoints) == 1, "the failing checkpoint write was actually attempted")
check(not reached and not obs,
      "a system DM is still dropped when the checkpoint write raises")

# The real shape, against the library's own enum rather than a hand-written int.
if HAVE_DISCORD:
    import discord as _real
    obs, reached = _run(_guild_message(system=True, mtype=_real.MessageType.thread_created))
    check(not reached, "a real THREAD_CREATED notice stops at the guard")
    check(not obs, "a real THREAD_CREATED notice is never observed for moderation")

    # thread_starter_message is the dangerous direction: it IS user content.
    obs, reached = _run(_guild_message(system=False,
                                       mtype=_real.MessageType.thread_starter_message))
    check(bool(obs), "a thread_starter_message still reaches the moderation observer")

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
