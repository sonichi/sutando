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
# Discord exception types — bridge's sender-auth gate catches NotFound /
# Forbidden / HTTPException explicitly for the fetch_member fallback path.
class _DiscordException(Exception): pass
class _HTTPException(_DiscordException): pass
class _NotFound(_HTTPException): pass
class _Forbidden(_HTTPException): pass
_discord_stub.DiscordException = _DiscordException
_discord_stub.HTTPException = _HTTPException
_discord_stub.NotFound = _NotFound
_discord_stub.Forbidden = _Forbidden
# ChannelType — match the subset bridge whitelists for DM-origin escalation
_discord_stub.ChannelType = types.SimpleNamespace(
    text="text",
    public_thread="public_thread",
    private_thread="private_thread",
    news_thread="news_thread",
    news="news",
    voice="voice",
    category="category",
    forum="forum",
)
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
    def __init__(self, channel_id, guild=None, name="ch", ch_type="text"):
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.type = ch_type  # mirror discord.Channel.type — used by escalate's DM-origin gate
        self.sent = []
    async def send(self, content, allowed_mentions=None):
        self.sent.append({"content": content, "allowed_mentions": allowed_mentions})


class _MockGuild:
    def __init__(self, gid, members=None, fetch_raises=None):
        self.id = gid
        # members: dict of {user_id: _MockMember}; sender-auth gate looks up
        # the sender via ch_guild.get_member(sender_id) (cache hit) then
        # falls back to ch_guild.fetch_member(...) (HTTP, can raise).
        self._members = members or {}
        # fetch_raises: optional Exception instance to raise from fetch_member
        # — used to test the NotFound / Forbidden / HTTPException paths.
        self._fetch_raises = fetch_raises
    def get_member(self, user_id):
        return self._members.get(user_id)
    async def fetch_member(self, user_id):
        if self._fetch_raises is not None:
            raise self._fetch_raises
        # discord.py's real fetch_member raises NotFound for non-members; the
        # mock matches that behavior when fetch_raises is unset and member
        # is missing.
        if user_id not in self._members:
            raise _NotFound("user not in guild")
        return self._members[user_id]


class _MockMember:
    def __init__(self, user_id, roles=()):
        self.id = user_id
        self.roles = roles


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
    # DM origin (msg.guild is None), task references a guild text channel that's
    # mod_active=True AND sender is a member. Should resolve target guild + post.
    sender_id = 999  # _MockMessage default author_id
    referenced_guild = _MockGuild(99, members={sender_id: _MockMember(sender_id)})
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="text")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: ({"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None} if gid == 99 else {"escalation_channel": None, "escalation_ccs": (), "redirect_channel_jobs": None})
    bridge._load_mod_config = lambda gid: ((True, []) if gid == 99 else (False, []))  # DM-origin gate 1: must be mod_active=True
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


def case_k_dm_origin_target_guild_not_mod_active():
    """Per MacBook #639 review #3: DM-origin path requires mod_active=True on
    the target guild. Otherwise an arbitrary user could DM a `<#...>` for any
    guild the bot is in and route their request to that guild's escalation
    channel — information leak vector. Gate enforcement must NOT post."""
    fails = []
    sender_id = 999
    referenced_guild = _MockGuild(99, members={sender_id: _MockMember(sender_id)})
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="text")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None}
    # mod_active=False for the target guild — gate 1 should reject
    bridge._load_mod_config = lambda gid: (False, [])
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"k) gate-rejected DM origin should still return True (silent); got {result}")
    if esc_ch.sent:
        fails.append("k) gate-rejected DM origin should NOT post to escalation channel")
    return fails


def case_l_dm_origin_referenced_channel_wrong_type():
    """Per MacBook #639 review #3: DM-origin path must reject non-guild-text
    channels (voice/category/etc.). Even if mod_active=True, a category or
    voice channel reference shouldn't drive routing."""
    fails = []
    sender_id = 999
    referenced_guild = _MockGuild(99, members={sender_id: _MockMember(sender_id)})
    # Non-text channel type
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="category")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None}
    bridge._load_mod_config = lambda gid: (True, [])  # mod_active=True but channel type wrong
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"l) wrong-type referenced channel should still return True silently; got {result}")
    if esc_ch.sent:
        fails.append("l) wrong-type referenced channel should NOT post to escalation channel")
    return fails


def case_n_dm_origin_sender_not_member_rejected():
    """Per MacBook #639 v2 follow-up review: `mod_active=True` is an opt-in
    gate, NOT a sender-auth gate. A team-tier-trusted DM sender is "trusted
    by Sutando" but that doesn't extend to driving escalations into a guild
    they're not a member of. DM-origin gate 2: reject if sender is not a
    member of the target guild.

    The mock's `fetch_member` raises `_NotFound` when sender is not in the
    `members` dict (mirrors discord.py's actual behavior — it raises
    `discord.NotFound`, NOT returns None). The bridge's gate must catch
    that explicitly to take the silent NO-REPLY path."""
    fails = []
    sender_id = 999
    # Target guild has mod_active=True BUT sender is NOT a member
    # _MockGuild.fetch_member will raise _NotFound (mirrors discord.NotFound)
    referenced_guild = _MockGuild(99, members={})
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="text")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None}
    bridge._load_mod_config = lambda gid: (True, [])  # passes gate 1, fails gate 2
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None, author_id=sender_id)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"n) non-member sender should still return True (silent); got {result}")
    if esc_ch.sent:
        fails.append("n) non-member sender should NOT trigger an escalation post (NotFound path)")
    return fails


def case_o_fetch_member_forbidden_silent():
    """Per MacBook v3 review: `fetch_member` can raise `Forbidden` (bot
    lacks permission to view members of that guild). Fail-closed: no post,
    no public reply."""
    fails = []
    sender_id = 999
    referenced_guild = _MockGuild(99, members={}, fetch_raises=_Forbidden("bot lacks Members intent"))
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="text")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None}
    bridge._load_mod_config = lambda gid: (True, [])
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None, author_id=sender_id)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"o) Forbidden should still return True (silent); got {result}")
    if esc_ch.sent:
        fails.append("o) Forbidden should NOT trigger an escalation post")
    return fails


def case_p_fetch_member_http_exception_silent():
    """Per MacBook v3 review: `fetch_member` can raise generic
    `HTTPException` (5xx, rate limit, etc.). Fail-closed."""
    fails = []
    sender_id = 999
    referenced_guild = _MockGuild(99, members={}, fetch_raises=_HTTPException("503 transient"))
    referenced_ch = _MockChannel(1307539994751139893, referenced_guild, ch_type="text")
    esc_ch = _MockChannel(1153753796321738863, referenced_guild, name="mod-only", ch_type="text")
    _install_mock_client({1307539994751139893: referenced_ch, 1153753796321738863: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 1153753796321738863, "escalation_ccs": (), "redirect_channel_jobs": None}
    bridge._load_mod_config = lambda gid: (True, [])
    dm_origin = _MockChannel(1501715985584099398, None)
    msg = _MockMessage("m", dm_origin, guild=None, author_id=sender_id)
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1307539994751139893>"))
    if result is not True:
        fails.append(f"p) HTTPException should still return True (silent); got {result}")
    if esc_ch.sent:
        fails.append("p) HTTPException should NOT trigger an escalation post")
    return fails


def case_q_extract_user_id_mentions_excludes_roles():
    """Per MacBook v4 line-level review: `<@&role_id>` strings would have
    crashed cc_ids extraction with ValueError under the old
    `s.startswith('<@')` check (the strip would yield `&123`, int() fails).
    `_extract_user_id_mentions` must accept user mentions and skip role
    mentions cleanly."""
    fails = []
    mixed = ["<@111>", "<@&222>", "<@333>", "garbage", "<@444>"]
    result = bridge._extract_user_id_mentions(mixed)
    expected = [111, 333, 444]
    if result != expected:
        fails.append(f"q1) expected user-only IDs {expected}, got {result}")
    if bridge._extract_user_id_mentions(()) != []:
        fails.append("q2) empty input should return []")
    if bridge._extract_user_id_mentions(None) != []:
        fails.append("q3) None input should return []")
    if bridge._extract_user_id_mentions(["<@&100>", "<@&200>"]) != []:
        fails.append("q4) role-only input should return []")
    return fails


def case_r_role_mention_in_escalation_ccs_does_not_crash():
    """End-to-end: when access.json has a role mention in escalation_ccs,
    the escalation post still goes through (role just doesn't appear in
    `allowed_mentions.users`). Pre-fix this would ValueError + escalation-fail."""
    fails = []
    guild = _MockGuild(42)
    origin_ch = _MockChannel(7777, guild)
    msg = _MockMessage("m", origin_ch, guild=guild)
    esc_ch = _MockChannel(9001, guild, name="mod-only")
    _install_mock_client({9001: esc_ch})
    bridge._load_mod_server_config = lambda gid: {
        "escalation_channel": 9001,
        "escalation_ccs": ("<@111>", "<@&999>", "<@222>"),
        "redirect_channel_jobs": None,
    }
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1234567890>"))
    if result is not True:
        fails.append(f"r) escalation with mixed cc list should still return True; got {result}")
    if not esc_ch.sent:
        fails.append("r) escalation should still post — role mentions must not crash the path")
    return fails


def case_m_guild_origin_unaffected_by_dm_gates():
    """Counter-case to k/l: when origin IS a guild channel, the DM-origin
    extra gates (mod_active check, channel-type validation) should NOT apply.
    Origin-guild path uses the message's own guild directly."""
    fails = []
    guild = _MockGuild(42)
    origin_ch = _MockChannel(7777, guild, ch_type="text")
    msg = _MockMessage("m", origin_ch, guild=guild)
    esc_ch = _MockChannel(9001, guild, name="mod-only", ch_type="text")
    _install_mock_client({9001: esc_ch})
    bridge._load_mod_server_config = lambda gid: {"escalation_channel": 9001, "escalation_ccs": (), "redirect_channel_jobs": None}
    # Even with mod_active=False, origin-guild path should still post
    bridge._load_mod_config = lambda gid: (False, [])
    result = asyncio.run(bridge._silent_escalate_for_discord_state(msg, "look at <#1234567890>"))
    if result is not True:
        fails.append(f"m) origin-guild should return True (got {result})")
    if not esc_ch.sent:
        fails.append("m) origin-guild path should post regardless of mod_active gate (DM-only gate)")
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
        ("k-dm-origin-mod-active-false-rejected", case_k_dm_origin_target_guild_not_mod_active),
        ("l-dm-origin-wrong-channel-type-rejected", case_l_dm_origin_referenced_channel_wrong_type),
        ("m-guild-origin-unaffected-by-dm-gates", case_m_guild_origin_unaffected_by_dm_gates),
        ("n-dm-origin-sender-not-member-rejected", case_n_dm_origin_sender_not_member_rejected),
        ("o-fetch-member-forbidden-silent", case_o_fetch_member_forbidden_silent),
        ("p-fetch-member-http-exception-silent", case_p_fetch_member_http_exception_silent),
        ("q-extract-user-id-mentions-excludes-roles", case_q_extract_user_id_mentions_excludes_roles),
        ("r-role-mention-in-escalation-ccs-does-not-crash", case_r_role_mention_in_escalation_ccs_does_not_crash),
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
