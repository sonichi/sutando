#!/usr/bin/env python3
"""
Tests for the per-rule action dispatchers in discord-bridge.py.

PR3 of 4 — covers `_post_mod_escalation`, `_action_delete_and_escalate`,
`_action_redirect_to_jobs`, `_action_escalate_only`, `_action_polite_reminder`.
The buffer + on_message wiring + Rule-3/Rule-7 stateful detectors land in PR4.

Tests use mocked discord.py message + channel objects so no real Discord
API hits.

Run: python3 tests/discord-bridge-mod-judge-actions.test.py
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
    def __init__(self, *args, **kwargs):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
    def event(self, fn): return fn
    def get_channel(self, _id): return None


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None


class _DMChannel: pass


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


# Per-guild mod config fixture: provide channels + cc list that the action
# functions look up via `_load_mod_server_config(guild_id)`. Replaces real
# access.json read so tests don't need a populated file.
_TEST_ESCALATION_CHANNEL_ID = 1153753796321738863
_TEST_JOBS_CHANNEL_ID = 1168719200605437952
_TEST_ESCALATION_CCS = ("<@1022910063620390932>", "<@1025828152183885925>", "<@786410785197785088>")


def _fake_server_config(guild_id):
    return {
        "escalation_channel": _TEST_ESCALATION_CHANNEL_ID,
        "escalation_ccs": _TEST_ESCALATION_CCS,
        "redirect_channel_jobs": _TEST_JOBS_CHANNEL_ID,
    }


bridge._load_mod_server_config = _fake_server_config


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _MockChannel:
    """Captures every .send() call. Returns a sentinel message object."""
    def __init__(self, name="general", channel_id=999):
        self.name = name
        self.id = channel_id
        self.sent = []  # list of strings sent
        self.send_should_raise = None
    async def send(self, content):
        if self.send_should_raise:
            raise self.send_should_raise
        self.sent.append(content)
        return types.SimpleNamespace(id=12345, content=content)


class _MockMessage:
    """Captures .delete() calls."""
    def __init__(self, msg_id="111", author_id=999, author_name="alice",
                 channel=None, content="hi", jump_url="https://discord/jump"):
        self.id = msg_id
        self.author = types.SimpleNamespace(id=author_id, display_name=author_name)
        self.channel = channel or _MockChannel()
        self.content = content
        self.jump_url = jump_url
        self.deleted = False
        self.delete_should_raise = None
    async def delete(self):
        if self.delete_should_raise:
            raise self.delete_should_raise
        self.deleted = True


def _client_with_mod_channel(mock_mod_ch):
    """Return a client mock where get_channel(_TEST_ESCALATION_CHANNEL_ID) returns mock_mod_ch."""
    def get_channel(cid):
        if cid == _TEST_ESCALATION_CHANNEL_ID:
            return mock_mod_ch
        return None
    return types.SimpleNamespace(get_channel=get_channel)


# ---------------------------------------------------------------------------
# _post_mod_escalation
# ---------------------------------------------------------------------------

def case_post_mod_escalation_basic() -> list[str]:
    fails = []
    mod_ch = _MockChannel(name="moderator-only")
    client = _client_with_mod_channel(mod_ch)
    msg = _MockMessage(content="some bad message")
    asyncio.run(bridge._post_mod_escalation(client, msg, "rule_1", "crypto-spam pattern"))
    if not mod_ch.sent:
        fails.append("a) escalation should post to mod channel")
        return fails
    posted = mod_ch.sent[0]
    if "rule_1" not in posted:
        fails.append("a) post should include rule label")
    if "crypto-spam pattern" not in posted:
        fails.append("a) post should include rationale")
    if "<@1022910063620390932>" not in posted:
        fails.append("a) post should cc Chi snowflake")
    if "<@786410785197785088>" not in posted:
        fails.append("a) post should cc msze snowflake")
    if "<@1025828152183885925>" not in posted:
        fails.append("a) post should cc Qingyun snowflake")
    return fails


def case_post_mod_escalation_no_channel_in_cache() -> list[str]:
    """If client.get_channel returns None (channel not in cache), don't crash."""
    fails = []
    client = types.SimpleNamespace(get_channel=lambda _id: None)
    msg = _MockMessage()
    result = asyncio.run(bridge._post_mod_escalation(client, msg, "rule_1", ""))
    if result is not None:
        fails.append("b) should return None when mod channel not cached")
    return fails


def case_post_mod_escalation_includes_extras() -> list[str]:
    fails = []
    mod_ch = _MockChannel()
    client = _client_with_mod_channel(mod_ch)
    msg = _MockMessage()
    asyncio.run(bridge._post_mod_escalation(client, msg, "rule_3", "raid pattern",
                                             extras_md="Cross-channel duplicates: #a, #b, #c"))
    posted = mod_ch.sent[0]
    if "Cross-channel duplicates" not in posted:
        fails.append("c) extras_md should appear in post")
    return fails


# ---------------------------------------------------------------------------
# _action_delete_and_escalate
# ---------------------------------------------------------------------------

def case_action_delete_and_escalate_happy() -> list[str]:
    fails = []
    mod_ch = _MockChannel()
    client = _client_with_mod_channel(mod_ch)
    msg = _MockMessage(content="crypto job spam $X/hour DM @everyone")
    verdict = {"msg_id": "111", "rule_match": "rule_1", "confidence": 0.92,
               "rationale": "crypto-job-listing"}
    deleted, esc = asyncio.run(bridge._action_delete_and_escalate(client, msg, verdict))
    if not deleted:
        fails.append("d) message should be deleted")
    if not msg.deleted:
        fails.append("d) message.delete() should have been called")
    if not mod_ch.sent:
        fails.append("d) escalation post should have been made")
    return fails


def case_action_delete_and_escalate_delete_fails() -> list[str]:
    """If delete fails (e.g. msg already gone), still post the escalation."""
    fails = []
    mod_ch = _MockChannel()
    client = _client_with_mod_channel(mod_ch)
    msg = _MockMessage()
    msg.delete_should_raise = Exception("404 Not Found")
    verdict = {"rule_match": "rule_2", "confidence": 0.91, "rationale": "csam-bait"}
    deleted, esc = asyncio.run(bridge._action_delete_and_escalate(client, msg, verdict))
    if deleted:
        fails.append("e) deleted should be False when delete raises")
    if not mod_ch.sent:
        fails.append("e) escalation should still post even if delete failed")
    return fails


# ---------------------------------------------------------------------------
# _action_redirect_to_jobs
# ---------------------------------------------------------------------------

def case_action_redirect_to_jobs_happy() -> list[str]:
    fails = []
    src_ch = _MockChannel(name="ag-for-mobile", channel_id=500)
    client = types.SimpleNamespace(get_channel=lambda _id: None)
    msg = _MockMessage(channel=src_ch, content="iOS dev DM me")
    verdict = {"rule_match": "rule_4", "confidence": 0.88, "rationale": "job-availability"}
    deleted, redirect = asyncio.run(bridge._action_redirect_to_jobs(client, msg, verdict))
    if not deleted:
        fails.append("f) message should be deleted")
    if not src_ch.sent:
        fails.append("f) redirect should be posted in same channel")
        return fails
    redirect_text = src_ch.sent[0]
    if f"<#{_TEST_JOBS_CHANNEL_ID}>" not in redirect_text:
        fails.append("f) redirect should mention #jobs channel by id")
    if f"<@{msg.author.id}>" not in redirect_text:
        fails.append("f) redirect should @-mention the original author")
    return fails


# ---------------------------------------------------------------------------
# _action_escalate_only
# ---------------------------------------------------------------------------

def case_action_escalate_only_no_delete() -> list[str]:
    fails = []
    mod_ch = _MockChannel()
    client = _client_with_mod_channel(mod_ch)
    msg = _MockMessage(content="possibly harassing message")
    verdict = {"rule_match": "rule_5", "confidence": 0.93, "rationale": "personal attack"}
    asyncio.run(bridge._action_escalate_only(client, msg, verdict))
    if msg.deleted:
        fails.append("g) escalate-only should NOT delete the suspect message")
    if not mod_ch.sent:
        fails.append("g) escalate-only should post to #moderator-only")
        return fails
    posted = mod_ch.sent[0]
    if "rule_5" not in posted:
        fails.append("g) post should include rule label")
    return fails


# ---------------------------------------------------------------------------
# _action_polite_reminder
# ---------------------------------------------------------------------------

def case_action_polite_reminder() -> list[str]:
    fails = []
    ch = _MockChannel(name="ag-for-finance")
    asyncio.run(bridge._action_polite_reminder(ch))
    if not ch.sent:
        fails.append("h) polite reminder should post in the same channel")
        return fails
    body = ch.sent[0]
    if "drifting" not in body and "drift" not in body:
        fails.append("h) reminder should mention drift / off-topic")
    if "<@" in body:
        fails.append("h) reminder should NOT @-mention anyone (don't single out)")
    return fails


def case_action_polite_reminder_send_fails() -> list[str]:
    """Polite reminder send failure should not raise — return None."""
    fails = []
    ch = _MockChannel()
    ch.send_should_raise = Exception("permission denied")
    out = asyncio.run(bridge._action_polite_reminder(ch))
    if out is not None:
        fails.append("i) failed reminder should return None")
    return fails


def main() -> int:
    cases = [
        ("a-escalate-basic", case_post_mod_escalation_basic),
        ("b-no-cache", case_post_mod_escalation_no_channel_in_cache),
        ("c-extras", case_post_mod_escalation_includes_extras),
        ("d-del+esc-happy", case_action_delete_and_escalate_happy),
        ("e-del-fails", case_action_delete_and_escalate_delete_fails),
        ("f-redirect", case_action_redirect_to_jobs_happy),
        ("g-escalate-only", case_action_escalate_only_no_delete),
        ("h-reminder", case_action_polite_reminder),
        ("i-reminder-fails", case_action_polite_reminder_send_fails),
    ]
    failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll mod-judge action-dispatcher invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
