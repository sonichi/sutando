#!/usr/bin/env python3
"""
Tests for the Discord-state-reference pre-classification in
`discord-bridge.py`. Per msze_'s 2026-05-07 directive: when a non-owner
task body references Discord-side state codex sandbox cannot read (channel
mentions like `<#1234>`), the bridge silently escalates to the appropriate
guild's `escalation_channel` and writes an "already_escalated" tier
instruction telling the agent to NO-REPLY archive — instead of letting
the agent emit the cold "Sandbox unavailable" string publicly.

Cases:
  Pure detection — `_extract_referenced_channels`:
    a) plain text with no `<#...>` → []
    b) single `<#1234>` → [1234]
    c) multiple `<#...>` → all in order
    d) malformed `<#abc>` (non-digit) → ignored
    e) empty / None → []

  Silent-escalate flow — `_silent_escalate_for_discord_state`:
    f) no `<#...>` references → returns False (caller proceeds normal)
    g) reference + guild channel + escalation_channel configured → posts
       to that channel, returns True
    h) reference + DM origin + referenced channel resolves to a guild with
       config → posts to THAT guild's escalation channel, returns True
    i) reference + no escalation_channel → returns True (silent NO-REPLY,
       no post attempted)
    j) reference + guild has no `mod_server_config` entry at all → True

Run: python3 tests/discord-bridge-discord-state-detection.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub minimal discord module
_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *a, **kw):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
        self._channels = {}
    def event(self, fn): return fn
    def get_channel(self, cid): return self._channels.get(cid)
    async def fetch_channel(self, cid): return self._channels.get(cid)


class _AllowedMentions:
    def __init__(self, *a, **kw): pass


class _DMChannel: pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.AllowedMentions = _AllowedMentions
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None
_discord_stub.DMChannel = _DMChannel

sys.modules["discord"] = _discord_stub


def load_bridge():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    fake_env_dir = Path.home() / ".claude" / "channels" / "discord"
    fake_env = fake_env_dir / ".env"
    if not fake_env.exists():
        fake_env_dir.mkdir(parents=True, exist_ok=True)
        fake_env.write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(src, bridge.__dict__)
    return bridge


bridge = load_bridge()


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, channel_id, guild=None, name="ch"):
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.sent = []
    async def send(self, content, allowed_mentions=None):
        self.sent.append({"content": content, "allowed_mentions": allowed_mentions})


class _MockGuild:
    def __init__(self, gid):
        self.id = gid


class _MockMessage:
    def __init__(self, msg_id, channel, author_id=999, guild=None):
        self.id = msg_id
        self.author = types.SimpleNamespace(id=author_id, display_name="alice")
        self.channel = channel
        self.guild = guild


def _install_mock_client(mock_channels_by_id):
    """Replace `bridge.client` with a _Client whose get_channel/fetch_channel
    return mocks from the dict."""
    c = _Client()
    c._channels = mock_channels_by_id
    bridge.client = c


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_a_plain_text():
    fails = []
    if bridge._extract_referenced_channels("just a plain message, no refs") != []:
        fails.append("a) plain text should return []")
    return fails


def case_b_single_ref():
    fails = []
    refs = bridge._extract_referenced_channels("look at <#1234567890>")
    if refs != [1234567890]:
        fails.append(f"b) single ref expected [1234567890], got {refs}")
    return fails


def case_c_multiple_refs():
    fails = []
    refs = bridge._extract_referenced_channels("compare <#111> and <#222> with <#333>")
    if refs != [111, 222, 333]:
        fails.append(f"c) multiple refs expected [111,222,333], got {refs}")
    return fails


def case_d_malformed_ignored():
    fails = []
    refs = bridge._extract_referenced_channels("<#abc> isn't valid, but <#42> is")
    if refs != [42]:
        fails.append(f"d) malformed ignored, valid kept; expected [42], got {refs}")
    return fails


def case_e_empty():
    fails = []
    if bridge._extract_referenced_channels("") != []:
        fails.append("e1) empty string should return []")
    if bridge._extract_referenced_channels(None) != []:
        fails.append("e2) None should return []")
    return fails


def case_f_no_refs_returns_false():
    fails = []
    msg = _MockMessage("m", _MockChannel(7777, _MockGuild(42)), guild=_MockGuild(42))
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "no channel refs here"))
    if result is not False:
        fails.append(f"f) no refs should return False (got {result})")
    return fails


def case_g_guild_origin_with_config():
    fails = []
    guild = _MockGuild(42)
    origin_ch = _MockChannel(7777, guild)
    msg = _MockMessage("m", origin_ch, guild=guild)
    esc_ch = _MockChannel(9001, guild, name="mod-only")
    _install_mock_client({9001: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 9001, "escalation_ccs": (), "redirect_channel_jobs": None}
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1234567890>"))
    if result is not True:
        fails.append(f"g) ref + guild + config should return True (got {result})")
    if not esc_ch.sent:
        fails.append("g) escalation channel should have received a post")
    elif "<#1234567890>" not in esc_ch.sent[0]["content"]:
        fails.append("g) escalation post should mention the referenced channel")
    return fails


def case_h_dm_origin_resolves_via_referenced_channel():
    fails = []
    # DM origin (msg.guild is None), but task references a channel that resolves to a guild
    referenced_guild = _MockGuild(99)
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild)
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: ({"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None} if gid == 99 else {"escalation_channel": None, "escalation_ccs": (), "redirect_channel_jobs": None})
    dm_origin = _MockChannel(1501715985584099398, None)  # DM
    msg = _MockMessage("m", dm_origin, guild=None)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"h) DM ref-resolve should return True (got {result})")
    if not esc_ch.sent:
        fails.append("h) referenced-channel's guild's escalation channel should have received the post")
    return fails


def case_i_no_escalation_channel_returns_true_no_post():
    fails = []
    guild = _MockGuild(42)
    origin_ch = _MockChannel(7777, guild)
    msg = _MockMessage("m", origin_ch, guild=guild)
    _install_mock_client({})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": None, "escalation_ccs": (), "redirect_channel_jobs": None}
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1234567890>"))
    if result is not True:
        fails.append(f"i) no escalation_channel should still return True (silent NO-REPLY); got {result}")
    return fails


def case_j_no_target_guild_resolvable():
    fails = []
    # DM origin, referenced channel can't be resolved
    _install_mock_client({})  # nothing in cache, fetch returns None too
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#9999999999>"))
    if result is not True:
        fails.append(f"j) no target guild resolvable should still return True (silent); got {result}")
    return fails


def main():
    cases = [
        ("a-plain-text", case_a_plain_text),
        ("b-single-ref", case_b_single_ref),
        ("c-multiple-refs", case_c_multiple_refs),
        ("d-malformed-ignored", case_d_malformed_ignored),
        ("e-empty", case_e_empty),
        ("f-no-refs-returns-false", case_f_no_refs_returns_false),
        ("g-guild-origin-with-config", case_g_guild_origin_with_config),
        ("h-dm-origin-resolves-via-referenced-channel", case_h_dm_origin_resolves_via_referenced_channel),
        ("i-no-escalation-channel-returns-true-no-post", case_i_no_escalation_channel_returns_true_no_post),
        ("j-no-target-guild-resolvable", case_j_no_target_guild_resolvable),
    ]
    failures = []
    for label, fn in cases:
        fs = fn()
        if fs:
            failures.extend([(label, msg) for msg in fs])
            print(f"  ✗ case {label}")
            for f in fs:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll Discord-state pre-classify invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
