#!/usr/bin/env python3
"""Repeat offenders in the Discord auto-moderator (#6312): strikes, timeout, one combined escalation.

Run: python3 tests/discord-bridge-mod-repeat-offender.test.py
Exit 0 on pass, 1 on fail.
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

# Isolate and seed the channel config before importing the bridge (see the dispatcher test).
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-discord-repeat-")
_ccd_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_ccd_discord.mkdir(parents=True, exist_ok=True)
(_ccd_discord / "access.json").write_text('{"allowFrom": []}')
(_ccd_discord / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

REPO = Path(__file__).resolve().parent.parent

_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *args, **kwargs):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)

    def event(self, fn):
        return fn

    def get_channel(self, _id):
        return None


class _AllowedMentions:
    def __init__(self, **kw):
        self.kw = kw


class _DMChannel:
    pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None
_discord_stub.AllowedMentions = _AllowedMentions
_discord_stub.DMChannel = _DMChannel
sys.modules["discord"] = _discord_stub


def load_bridge():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


bridge = load_bridge()
if not Path(bridge.ACCESS_FILE).resolve().is_relative_to(Path(os.environ["CLAUDE_CONFIG_DIR"]).resolve()):
    sys.exit(f"refusing to run: ACCESS_FILE {bridge.ACCESS_FILE} is outside the test's config dir")

GUILD, ESC_CH, MOD_ROLE, OWNER, SPAMMER = 7001, 7002, 7003, 7004, 7005


def write_access(repeat=None, **extra):
    g = {"mod_active": True, "moderator_roles": [str(MOD_ROLE)], "escalation_channel": str(ESC_CH)}
    if repeat is not None:
        g["repeat_offender"] = repeat
    g.update(extra)
    bridge.ACCESS_FILE.write_text(json.dumps({"allowFrom": [], "guilds": {str(GUILD): g}}))


class Channel:
    def __init__(self, name="general", cid=1):
        self.name, self.id, self.sent = name, cid, []

    async def send(self, content, **kw):
        self.sent.append(content)
        return types.SimpleNamespace(id=len(self.sent), content=content)


class Guild:
    def __init__(self, can_moderate=True):
        self.id, self.owner_id = GUILD, OWNER
        self.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(moderate_members=can_moderate))


class Member:
    def __init__(self, guild, uid=SPAMMER, roles=(), refuse=None):
        self.id, self.guild, self.display_name = uid, guild, f"user{uid}"
        self.roles = [types.SimpleNamespace(id=r) for r in roles]
        self.timeouts, self.refuse = [], refuse

    async def timeout(self, duration, reason=None):
        if self.refuse:
            raise self.refuse
        self.timeouts.append((duration.total_seconds(), reason))


class Message:
    count = 0

    def __init__(self, author, channel, content="buy crypto jobs now"):
        Message.count += 1
        self.id, self.author, self.channel, self.content = Message.count, author, channel, content
        self.guild, self.jump_url, self.deleted = author.guild, "https://discord/jump", False

    async def delete(self):
        self.deleted = True


fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        fails.append(name)


def fresh():
    bridge._mod_strike_tracker = bridge._StrikeTracker()
    mod_ch = Channel("moderator-only", ESC_CH)
    return mod_ch, types.SimpleNamespace(get_channel=lambda cid: mod_ch if cid == ESC_CH else None)


def delete(client, msg, rule="rule_1", now_s=None):
    """One rule verdict that deletes, through the real action and the real strike path."""
    verdict = {"rule_match": rule, "confidence": 0.95, "rationale": "spam"}
    if now_s is None:
        return asyncio.run(bridge._action_delete_and_escalate(client, msg, verdict))
    real = bridge.time.time
    bridge.time.time = lambda: now_s
    try:
        return asyncio.run(bridge._action_delete_and_escalate(client, msg, verdict))
    finally:
        bridge.time.time = real


# --- off by default --------------------------------------------------------------------------
write_access()
mod_ch, client = fresh()
spammer = Member(Guild())
for _ in range(5):
    delete(client, Message(spammer, Channel()))
check("without repeat_offender, every deletion escalates as before", len(mod_ch.sent), 5)
check("and nobody is timed out", spammer.timeouts, [])

# --- below and at the limit ------------------------------------------------------------------
write_access({"strikes": 3, "window_s": 600, "timeout_s": 3600})
mod_ch, client = fresh()
spammer = Member(Guild())
msgs = [Message(spammer, Channel(f"chan{i}", i)) for i in range(3)]
delete(client, msgs[0], now_s=1000)
delete(client, msgs[1], now_s=1100)
check("two deletions: two ordinary escalations", [("repeat offender" in s) for s in mod_ch.sent], [False, False])
check("and no timeout below the limit", spammer.timeouts, [])
deleted_ok, esc = delete(client, msgs[2], now_s=1200)
check("the third deletion inside the window still deletes", (deleted_ok, msgs[2].deleted), (True, True))
check("it times the author out once, for timeout_s", [t for t, _r in spammer.timeouts], [3600.0])
check("and posts one combined escalation, not another per-message one", len(mod_ch.sent), 3)
combined = mod_ch.sent[-1]
check("which names the author", f"<@{SPAMMER}>" in combined, True)
check("lists each strike's channel", all(f"#chan{i}" in combined for i in range(3)), True)
check("and says when the timeout ends", f"<t:{1200 + 3600}:f>" in combined, True)
delete(client, Message(spammer, Channel()), now_s=1300)
check("the count starts again after a timeout", (len(spammer.timeouts), "repeat offender" in mod_ch.sent[-1]),
      (1, False))

# --- the window --------------------------------------------------------------------------------
mod_ch, client = fresh()
spammer = Member(Guild())
for at in (1000, 1300, 1700):
    delete(client, Message(spammer, Channel()), now_s=at)
check("strikes older than window_s do not count", spammer.timeouts, [])
delete(client, Message(spammer, Channel()), now_s=1800)
check("three inside one window do", len(spammer.timeouts), 1)

# --- never a moderator or the owner --------------------------------------------------------
for who, member in (("a moderator", Member(Guild(), uid=7010, roles=(MOD_ROLE,))),
                    ("the server owner", Member(Guild(), uid=OWNER))):
    mod_ch, client = fresh()
    for at in (1, 2, 3):
        delete(client, Message(member, Channel()), now_s=at)
    check(f"{who} is never timed out", member.timeouts, [])
    check(f"and the escalation says so for {who}", "author is a moderator or the server owner" in mod_ch.sent[-1], True)

# --- missing permission, or Discord refuses ------------------------------------------------
mod_ch, client = fresh()
spammer = Member(Guild(can_moderate=False))
for at in (1, 2, 3):
    delete(client, Message(spammer, Channel()), now_s=at)
check("without Moderate Members, no timeout is attempted", spammer.timeouts, [])
check("and the escalation says why", "lacks the Moderate Members permission" in mod_ch.sent[-1], True)

mod_ch, client = fresh()
spammer = Member(Guild(), refuse=PermissionError("Missing Permissions"))
for at in (1, 2, 3):
    out = delete(client, Message(spammer, Channel()), now_s=at)
check("a refused timeout raises nothing and still deletes", out[0], True)
check("and the escalation reports the refusal", "Discord refused it (PermissionError" in mod_ch.sent[-1], True)

# --- what counts --------------------------------------------------------------------------
mod_ch, client = fresh()
spammer = Member(Guild())


class Undeletable(Message):
    async def delete(self):
        raise RuntimeError("404 Unknown Message")


for at in (1, 2, 3):
    delete(client, Undeletable(spammer, Channel()), now_s=at)
check("a message the bot failed to delete is not a strike", spammer.timeouts, [])

mod_ch, client = fresh()
spammer = Member(Guild())
for at in (1, 2, 3):
    m = Message(spammer, Channel())
    asyncio.run(bridge._action_escalate_only(client, m, {"rule_match": "rule_5", "confidence": 0.99}))
    asyncio.run(bridge._action_redirect_to_jobs(client, m, {"rule_match": "rule_4", "confidence": 0.99}))
check("escalate-only and the rule 4 redirect are never strikes", spammer.timeouts, [])

other_guild_author = Member(Guild())
other_guild_author.guild.id = 9999
other_guild_author.guild.me = Guild().me
mod_ch, client = fresh()
for at in (1, 2, 3):
    delete(client, Message(other_guild_author, Channel()), now_s=at)
check("a guild without repeat_offender is unaffected", other_guild_author.timeouts, [])

# --- configuration --------------------------------------------------------------------------
write_access({})
check("an empty repeat_offender takes the defaults", bridge._load_repeat_offender_config(GUILD),
      bridge.REPEAT_OFFENDER_DEFAULTS)
for bad in ({"strikes": 1}, {"strikes": True}, {"window_s": "600"}, {"timeout_s": 29 * 24 * 3600}):
    write_access(bad)
    check(f"{bad} turns it off", bridge._load_repeat_offender_config(GUILD), None)
write_access("yes")
check("a repeat_offender that is not an object is off", bridge._load_repeat_offender_config(GUILD), None)
bridge.ACCESS_FILE.write_text("{")
check("an unreadable access.json is off", bridge._load_repeat_offender_config(GUILD), None)

# --- memory stays bounded --------------------------------------------------------------------
tracker = bridge._StrikeTracker()
tracker.add(GUILD, 1, {}, 0, 600, 3)
tracker.add(GUILD, 2, {}, 90000, 600, 3)
tracker.gc(now_s=90000)
check("gc drops authors no window can reach", sorted(k[1] for k in tracker._store), ["2"])

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — repeat offenders")
sys.exit(1 if fails else 0)
