#!/usr/bin/env python3
"""
Integration tests for the AG2 auto-mod LLM-judge end-to-end pipeline:
observe → buffer → flush → parse → dispatch. Specifically exercises the
wiring that earlier unit tests didn't cover (Rule 3 dupe context, Rule 7
streak feed, batch retention on judge failure, mention sanitization).

These cases caught bugs in the original PR #633 (per MacBook cold-review):
- batch silently dropped on codex failure
- Rule 3 wiring dead (DupeTracker.add never called)
- Rule 7 dispatcher path skipped instead of feeding tracker
- violates_server_rules field stripped at parse time
- mention-injection in escalation post

Run: python3 tests/discord-bridge-mod-judge-integration.test.py
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
    def event(self, fn): return fn
    def get_channel(self, _id): return None


class _AllowedMentions:
    def __init__(self, *a, **kw): pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.AllowedMentions = _AllowedMentions
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

# Per-guild fixture so escalation/redirect resolve cleanly
def _fake_server_config(guild_id):
    return {
        "escalation_channel": 9001,
        "escalation_ccs": ("<@111>", "<@222>"),
        "redirect_channel_jobs": 8002,
    }


bridge._load_mod_server_config = _fake_server_config


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, name="general", channel_id=999):
        self.name = name
        self.id = channel_id
        self.sent = []
    async def send(self, content, allowed_mentions=None):
        self.sent.append({"content": content, "allowed_mentions": allowed_mentions})
        return types.SimpleNamespace(id=12345)


class _MockMessage:
    def __init__(self, msg_id, channel, author_id=999, author_name="alice", content="", guild_id=42):
        self.id = msg_id
        self.author = types.SimpleNamespace(id=author_id, display_name=author_name)
        self.channel = channel
        self.content = content
        self.guild = types.SimpleNamespace(id=guild_id)
        self.jump_url = f"https://discord/jump/{msg_id}"
        self.deleted = False
    async def delete(self):
        self.deleted = True


def _client_with_channel(mock_ch, ch_id):
    def get_channel(cid):
        return mock_ch if cid == ch_id else None
    return types.SimpleNamespace(get_channel=get_channel)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_a_batch_retained_on_codex_failure():
    """Buffer must be preserved when codex raises. Original bug: buffer
    cleared before codex call, so failure = silent loss of 20 messages."""
    fails = []
    bridge._mod_buffer.clear()
    ch = _MockChannel("general", 9999)
    msg = _MockMessage("m1", ch, content="hello")

    # Inject a synthetic record into the buffer (skip async observe)
    bridge._mod_buffer.append({
        "msg_id": "m1", "channel_name": "general", "channel_id": 9999,
        "author_name": "alice", "author_id": 999, "content": "hello",
        "is_reply": False, "parent_content": "", "_msg": msg,
    })
    # Force codex to fail
    async def boom(*a, **kw):
        raise RuntimeError("codex timeout")
    orig = bridge._codex_judge_batch
    bridge._codex_judge_batch = boom
    try:
        asyncio.run(bridge._flush_mod_buffer())
        if len(bridge._mod_buffer) != 1:
            fails.append(f"a) batch should remain in buffer on codex failure (got {len(bridge._mod_buffer)} items)")
    finally:
        bridge._codex_judge_batch = orig
        bridge._mod_buffer.clear()
    return fails


def case_b_batch_retained_on_empty_verdicts():
    """Buffer must be preserved when codex returns no verdicts (malformed
    output). Original bug: empty list → silent buffer clear."""
    fails = []
    bridge._mod_buffer.clear()
    ch = _MockChannel("general", 9999)
    msg = _MockMessage("m1", ch, content="hello")
    bridge._mod_buffer.append({
        "msg_id": "m1", "channel_name": "general", "channel_id": 9999,
        "author_name": "alice", "author_id": 999, "content": "hello",
        "is_reply": False, "parent_content": "", "_msg": msg,
    })
    async def empty(*a, **kw):
        return []
    orig = bridge._codex_judge_batch
    bridge._codex_judge_batch = empty
    try:
        asyncio.run(bridge._flush_mod_buffer())
        if len(bridge._mod_buffer) != 1:
            fails.append(f"b) batch should remain in buffer when codex returns []")
    finally:
        bridge._codex_judge_batch = orig
        bridge._mod_buffer.clear()
    return fails


def case_c_batch_cleared_on_dispatch_success():
    """When dispatch completes, the judged messages should be removed from buffer."""
    fails = []
    bridge._mod_buffer.clear()
    ch = _MockChannel("general", 9999)
    msg = _MockMessage("m1", ch, content="just chatting")
    bridge._mod_buffer.append({
        "msg_id": "m1", "channel_name": "general", "channel_id": 9999,
        "author_name": "alice", "author_id": 999, "content": "just chatting",
        "is_reply": False, "parent_content": "", "_msg": msg,
    })
    async def clean_verdict(*a, **kw):
        return [{"msg_id": "m1", "rule_match": None, "confidence": 0.95, "rationale": "clean"}]
    orig_codex = bridge._codex_judge_batch
    bridge._codex_judge_batch = clean_verdict
    try:
        asyncio.run(bridge._flush_mod_buffer())
        if len(bridge._mod_buffer) != 0:
            fails.append(f"c) batch should clear after successful dispatch (got {len(bridge._mod_buffer)})")
    finally:
        bridge._codex_judge_batch = orig_codex
        bridge._mod_buffer.clear()
    return fails


def case_d_rule3_dupe_tracker_wired():
    """When 3 messages with same content land in 3 different channels in
    the same flush window, dupe-tracker should mark them as Rule 3 candidates
    and the prompt should carry rule_3 context."""
    fails = []
    bridge._mod_buffer.clear()
    bridge._mod_dupe_tracker = bridge._DupeTracker()
    captured = {"rules_context": None}
    async def capture(messages_for_judge, rules_context="", **kw):
        captured["rules_context"] = rules_context
        # All Rule 3 candidates should also be in the messages list
        return [{"msg_id": m["msg_id"], "rule_match": None, "confidence": 0.9, "rationale": "ok"}
                for m in messages_for_judge]
    orig = bridge._codex_judge_batch
    bridge._codex_judge_batch = capture
    try:
        for i, ch_id in enumerate([100, 200, 300]):
            ch = _MockChannel(f"ch{i}", ch_id)
            msg = _MockMessage(f"m{i}", ch, content="join my crypto server discord.gg/xyz")
            bridge._mod_buffer.append({
                "msg_id": f"m{i}", "channel_name": f"ch{i}", "channel_id": ch_id,
                "author_name": "spammer", "author_id": 999, "content": "join my crypto server discord.gg/xyz",
                "is_reply": False, "parent_content": "", "_msg": msg,
            })
        asyncio.run(bridge._flush_mod_buffer())
        if not captured["rules_context"]:
            fails.append("d) dupe-tracker should add+detect duplicates and inject rule_3 context into prompt")
        elif "rule_3" not in captured["rules_context"]:
            fails.append(f"d) rules_context should mention rule_3 (got: {captured['rules_context'][:80]})")
    finally:
        bridge._codex_judge_batch = orig
        bridge._mod_buffer.clear()
    return fails


def case_e_rule7_feeds_streak_tracker():
    """When dispatcher receives a rule_7 verdict, it should feed off_topic=True
    into the streak tracker (originally the path skipped without feeding)."""
    fails = []
    bridge._mod_streak_tracker = bridge._OffTopicStreakTracker()
    ch = _MockChannel("focused-channel", 4242)
    msg = _MockMessage("m1", ch, content="random off-topic chat", author_id=777)
    verdicts = [{"msg_id": "m1", "rule_match": "rule_7", "confidence": 0.95, "rationale": "off-topic"}]
    asyncio.run(bridge._dispatch_verdicts(
        verdicts, {"m1": msg}, client_ref=None,
        off_topic_tracker=bridge._mod_streak_tracker,
    ))
    # After 1 rule_7 verdict, the tracker should have 1 off-topic entry for this channel.
    snapshot = bridge._mod_streak_tracker._streaks.get("4242", [])
    if not snapshot:
        fails.append("e) rule_7 verdict should feed an entry into _OffTopicStreakTracker")
    elif not any(e.get("off_topic") for e in snapshot):
        fails.append("e) rule_7 verdict should feed off_topic=True into tracker")
    return fails


def case_f_violates_server_rules_preserved():
    """_parse_judge_output must preserve `violates_server_rules` so the
    dispatcher's rule_3 escape hatch is reachable."""
    fails = []
    raw = (
        '{"verdicts":[{"msg_id":"m1","rule_match":"rule_3",'
        '"confidence":0.9,"rationale":"legit cross-post",'
        '"violates_server_rules":false}]}'
    )
    parsed = bridge._parse_judge_output(raw)
    if not parsed:
        fails.append("f) parser should return verdict for valid rule_3 output")
        return fails
    if "violates_server_rules" not in parsed[0]:
        fails.append("f) violates_server_rules field should be preserved by parser")
    elif parsed[0]["violates_server_rules"] is not False:
        fails.append(f"f) violates_server_rules=false should preserve as False (got {parsed[0]['violates_server_rules']})")
    return fails


def case_g_rule3_legit_crosspost_escalates_only():
    """When rule_3 verdict has violates_server_rules=False, dispatcher
    should escalate-only (NOT delete). Originally the escape hatch was
    unreachable because parser stripped the field."""
    fails = []
    mod_ch = _MockChannel("mod-only", 9001)
    client = _client_with_channel(mod_ch, 9001)
    ch = _MockChannel("ann-1", 4321)
    msg = _MockMessage("m1", ch, content="Event Friday at 3pm!")
    verdicts = [{
        "msg_id": "m1", "rule_match": "rule_3", "confidence": 0.95,
        "rationale": "legit cross-post", "violates_server_rules": False,
    }]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"m1": msg}, client_ref=client))
    if msg.deleted:
        fails.append("g) Rule 3 with violates_server_rules=False should NOT delete the message")
    if not mod_ch.sent:
        fails.append("g) Rule 3 with violates_server_rules=False should escalate-only (post to mod channel)")
    return fails


def case_h_mention_injection_sanitized():
    """Suspect message containing @everyone should not produce a real ping
    in the escalation post. Body should also use allowed_mentions to limit
    to cc users only."""
    fails = []
    mod_ch = _MockChannel("mod-only", 9001)
    client = _client_with_channel(mod_ch, 9001)
    ch = _MockChannel("general", 7777)
    msg = _MockMessage("m1", ch, content="@everyone free crypto here", author_id=999)
    asyncio.run(bridge._post_mod_escalation(client, msg, "rule_1", "spam pattern"))
    if not mod_ch.sent:
        fails.append("h) escalation should still post even with hostile content")
        return fails
    posted = mod_ch.sent[0]["content"]
    if "@everyone" in posted and "@\u200beveryone" not in posted:
        fails.append("h) @everyone should be sanitized (zero-width space inserted after @)")
    am = mod_ch.sent[0]["allowed_mentions"]
    if am is None:
        fails.append("h) allowed_mentions should be set on the escalation send")
    return fails


def case_i_concurrent_flush_single_flight():
    """v2 HIGH: Two parallel `_flush_mod_buffer()` calls must not double-judge
    the same batch. Original bug (pre-fix): snapshot under buffer-lock, release
    for codex, only clear after dispatch — so a second flush snapshots the same
    still-populated buffer and the same messages get judged twice. Fix uses a
    flush-level lock for single-flight execution.

    Test: launch 2 flushes concurrently with a slow codex stub; assert codex
    is invoked exactly once with all messages, and the buffer ends empty."""
    fails = []
    bridge._mod_buffer.clear()
    # Reset locks so a fresh asyncio event loop can recreate them
    bridge._mod_buffer_lock = None
    bridge._mod_flush_lock = None
    ch = _MockChannel("general", 9999)
    msgs = []
    for i in range(3):
        m = _MockMessage(f"m{i}", ch, content=f"hello {i}", author_id=900 + i)
        msgs.append(m)
        bridge._mod_buffer.append({
            "msg_id": f"m{i}", "channel_name": "general", "channel_id": 9999,
            "author_name": f"user{i}", "author_id": 900 + i, "content": f"hello {i}",
            "is_reply": False, "parent_content": "", "_msg": m,
        })
    invocations = {"count": 0, "msg_ids_seen": []}

    async def slow_codex(messages_for_judge, rules_context="", **kw):
        invocations["count"] += 1
        invocations["msg_ids_seen"].append([m["msg_id"] for m in messages_for_judge])
        # Simulate codex latency so the concurrent flush has time to enter
        # and try to snapshot the same buffer.
        await asyncio.sleep(0.05)
        return [{"msg_id": m["msg_id"], "rule_match": None, "confidence": 0.9, "rationale": "ok"}
                for m in messages_for_judge]
    orig = bridge._codex_judge_batch
    bridge._codex_judge_batch = slow_codex
    try:
        async def run_two():
            await asyncio.gather(
                bridge._flush_mod_buffer(),
                bridge._flush_mod_buffer(),
            )
        asyncio.run(run_two())
        if invocations["count"] != 1:
            fails.append(f"i) codex should be invoked exactly once for one batch (got {invocations['count']})")
        if invocations["msg_ids_seen"] and len(invocations["msg_ids_seen"][0]) != 3:
            fails.append(f"i) first invocation should see all 3 messages (got {invocations['msg_ids_seen']})")
        if len(bridge._mod_buffer) != 0:
            fails.append(f"i) buffer should be cleared after successful dispatch (got {len(bridge._mod_buffer)})")
    finally:
        bridge._codex_judge_batch = orig
        bridge._mod_buffer.clear()
    return fails


def case_j_rule3_clears_after_trigger():
    """v2 MEDIUM: After a Rule 3 trigger fires and dispatch succeeds, the
    DupeTracker bucket for that (user_id, text) key must be cleared. Otherwise
    a follow-up post in the 5-min window inherits the old 3-channel evidence
    and re-triggers Rule 3 with weak evidence (1 new + 3 stale msgs).

    Test: trigger Rule 3, run flush, then assert the bucket is empty so a
    new post in a 4th channel does NOT immediately trigger again."""
    fails = []
    bridge._mod_buffer.clear()
    bridge._mod_dupe_tracker = bridge._DupeTracker()
    # Reset flush lock so this test's event loop creates a fresh one
    bridge._mod_buffer_lock = None
    bridge._mod_flush_lock = None
    spam_text = "JOIN MY CRYPTO discord.gg/zzz"
    user_id = 13579

    async def clean_codex(messages_for_judge, rules_context="", **kw):
        return [{"msg_id": m["msg_id"], "rule_match": "rule_3", "confidence": 0.95,
                 "rationale": "dupe", "violates_server_rules": True}
                for m in messages_for_judge]
    orig = bridge._codex_judge_batch
    bridge._codex_judge_batch = clean_codex
    try:
        # First batch: same user, same text, 3 distinct channels → fires Rule 3
        for i, ch_id in enumerate([100, 200, 300]):
            ch = _MockChannel(f"ch{i}", ch_id)
            m = _MockMessage(f"m{i}", ch, content=spam_text, author_id=user_id)
            bridge._mod_buffer.append({
                "msg_id": f"m{i}", "channel_name": f"ch{i}", "channel_id": ch_id,
                "author_name": "spammer", "author_id": user_id, "content": spam_text,
                "is_reply": False, "parent_content": "", "_msg": m,
            })
        asyncio.run(bridge._flush_mod_buffer())
        # Assert dupe-tracker bucket cleared for this (user, text)
        norm_key = (str(user_id), bridge._normalize_msg_text(spam_text))
        bucket = bridge._mod_dupe_tracker._store.get(norm_key)
        if bucket and len(bucket) > 0:
            fails.append(f"j) DupeTracker bucket for triggered (user,text) should be cleared "
                         f"after dispatch success (got {len(bucket)} entries: {bucket})")
        # And: a follow-up post in a 4th channel should NOT immediately re-fire,
        # because the bucket is fresh — only 1 entry, well under threshold of 3.
        import time as _t
        trigger = bridge._mod_dupe_tracker.add(
            user_id=user_id, channel_id=400, msg_id="m_follow",
            text=spam_text, now_s=_t.time(),
        )
        if trigger:
            fails.append(f"j) a single follow-up post should NOT re-trigger Rule 3 "
                         f"after the bucket was cleared (got trigger={trigger})")
    finally:
        bridge._codex_judge_batch = orig
        bridge._mod_buffer.clear()
    return fails


def case_k_mod_message_resets_offtopic_streak():
    """v2 MEDIUM: A moderator posting in an mod-active channel must reset the
    Rule 7 off-topic streak. Original bugs: (1) `_observe_for_mod` returns
    silently for mods so they never reach the streak tracker; (2) the dispatcher
    clean-verdict path read `getattr(v, "_is_mod", False)` on a dict (always
    False). Fix: `_observe_for_mod` feeds the streak tracker directly with
    is_mod=True for mod authors.

    Test: pre-load a streak with off-topic entries, then call _observe_for_mod
    with a mod author; assert the channel's streak buffer is empty afterwards.
    """
    fails = []
    bridge._mod_streak_tracker = bridge._OffTopicStreakTracker()
    # Reset buffer lock so the observe call's event loop creates a fresh one
    bridge._mod_buffer_lock = None
    bridge._mod_flush_lock = None
    bridge._mod_buffer.clear()
    channel_id = 5555
    # Pre-load streak with 3 off-topic entries from a regular user
    import time as _t
    now = _t.time()
    for i in range(3):
        bridge._mod_streak_tracker.record(
            channel_id, user_id=999 + i, off_topic=True, is_mod=False, now_s=now,
        )
    pre = bridge._mod_streak_tracker._streaks.get(str(channel_id), [])
    if len(pre) != 3:
        fails.append(f"k) test setup: expected 3 pre-loaded streak entries, got {len(pre)}")
        return fails
    # Stub _load_mod_config + _is_moderator so the observe path classifies
    # the author as a mod and the guild as mod_active=True.
    orig_load = bridge._load_mod_config
    orig_is_mod = bridge._is_moderator
    bridge._load_mod_config = lambda gid: (True, ["mod-role-1"])
    bridge._is_moderator = lambda member, mod_role_ids: True
    try:
        ch = _MockChannel("general", channel_id)
        mod_msg = _MockMessage("m_mod", ch, content="please stay on-topic everyone",
                                author_id=42, author_name="modA")
        # Author needs roles attribute for _is_moderator path (but our stub
        # bypasses that — the real path is exercised in unit tests).
        mod_msg.author.roles = []
        asyncio.run(bridge._observe_for_mod(mod_msg))
        post = bridge._mod_streak_tracker._streaks.get(str(channel_id), [])
        if post and len(post) > 0:
            fails.append(f"k) mod message should reset channel streak to empty "
                         f"(got {len(post)} entries remaining)")
        # Mod messages also must NOT be added to the buffer (G1 immunity)
        if any(r.get("msg_id") == "m_mod" for r in bridge._mod_buffer):
            fails.append("k) mod message should NOT be added to mod buffer (G1)")
    finally:
        bridge._load_mod_config = orig_load
        bridge._is_moderator = orig_is_mod
        bridge._mod_buffer.clear()
    return fails


def main():
    cases = [
        ("a-batch-retained-on-codex-failure", case_a_batch_retained_on_codex_failure),
        ("b-batch-retained-on-empty-verdicts", case_b_batch_retained_on_empty_verdicts),
        ("c-batch-cleared-on-dispatch-success", case_c_batch_cleared_on_dispatch_success),
        ("d-rule3-dupe-tracker-wired", case_d_rule3_dupe_tracker_wired),
        ("e-rule7-feeds-streak-tracker", case_e_rule7_feeds_streak_tracker),
        ("f-violates-server-rules-preserved", case_f_violates_server_rules_preserved),
        ("g-rule3-legit-crosspost-escalates-only", case_g_rule3_legit_crosspost_escalates_only),
        ("h-mention-injection-sanitized", case_h_mention_injection_sanitized),
        ("i-concurrent-flush-single-flight", case_i_concurrent_flush_single_flight),
        ("j-rule3-clears-after-trigger", case_j_rule3_clears_after_trigger),
        ("k-mod-message-resets-offtopic-streak", case_k_mod_message_resets_offtopic_streak),
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
    print("\nAll mod-judge integration invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
