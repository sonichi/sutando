#!/usr/bin/env python3
"""Behavioral tests for the not-on-allowlist auto-ack (Discord + Slack).

When a sender ADDRESSES the bot (a DM, or an @mention) but isn't on the
allowlist, the access gate drops the message — historically silently. These
tests exercise the REAL `_ack_not_allowlisted` in each bridge (transport
stubbed) and assert Discord's durable per-(channel, sender) seven-day cooldown plus
Slack's existing per-sender cooldown and drop-path wiring.

Run: python3 tests/bridge-not-allowlisted-ack.test.py   (exit 0 pass / 1 fail)
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
FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  XX  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ─────────────────────────── Discord ───────────────────────────
def _load_discord():
    # Isolate import-time channel resolution before loading the bridge. Setting
    # ACCESS_FILE after import is too late: the module resolves/chmods its .env
    # and access.json while executing top-level code.
    ccd = Path(tempfile.mkdtemp(prefix="sutando-ack-discord-ccd-"))
    channel_dir = ccd / "channels" / "discord"
    channel_dir.mkdir(parents=True)
    (channel_dir / "access.json").write_text("{}\n")
    os.environ["CLAUDE_CONFIG_DIR"] = str(ccd)
    d = types.ModuleType("discord")
    class _Intents:
        def __init__(self, *a, **k): pass
        @classmethod
        def default(cls): return cls()
    class _Client:
        def __init__(self, *a, **k): pass
        def event(self, fn): return fn
        def get_channel(self, _): return None
    d.Intents = _Intents; d.Client = _Client
    d.MessageType = types.SimpleNamespace(default=0, reply=1)
    d.File = lambda *a, **k: None
    class _MessageReference:
        def __init__(
            self, *, message_id, channel_id, fail_if_not_exists=True, **_kwargs
        ):
            self.message_id = message_id
            self.channel_id = channel_id
            self.fail_if_not_exists = fail_if_not_exists
    d.MessageReference = _MessageReference
    d.HTTPException = type("_HTTPException", (Exception,), {})
    class _DM: pass
    d.DMChannel = _DM
    d.Thread = type("Thread", (), {})
    d.AllowedMentions = lambda **k: None
    sys.modules["discord"] = d
    # Inject the token via the process env (discord-bridge reads DISCORD_BOT_TOKEN
    # env-var-first and skips the .env file when it's set — see src/discord-bridge.py
    # ~L197/L209). Mirrors _load_slack below. NEVER seed the real
    # ~/.claude/channels/discord/.env — that mutates the contributor's live config
    # at import time (CR: qingyun-wu, #2109).
    os.environ.setdefault("DISCORD_BOT_TOKEN", "test-stub-token")
    src = (REPO / "src" / "discord-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("dbridge_ack", loader=None)
    b = importlib.util.module_from_spec(spec); b.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(compile(src, b.__file__, "exec"), b.__dict__)
    assert Path(b.channels_env).parent == channel_dir
    assert Path(b.ACCESS_FILE) == channel_dir / "access.json"
    return b


class _FakeChannel:
    def __init__(self, channel_id=123):
        self.id = channel_id; self.sent = []; self.references = []
    async def send(self, text, **kw):
        self.sent.append(text); self.references.append(kw.get("reference"))


class _YieldingChannel(_FakeChannel):
    async def send(self, text, **kw):
        await asyncio.sleep(0)
        self.sent.append(text); self.references.append(kw.get("reference"))


async def _run_concurrently(*awaitables):
    await asyncio.gather(*awaitables)


def test_discord():
    db = _load_discord()
    # CRITICAL: redirect ACCESS_FILE to a throwaway temp path so this test can
    # NEVER read/write the REAL ~/.claude/channels/discord/access.json. Every
    # reader (load_allowed / load_channel_config / the handler) uses this module
    # global, so reassigning it fully isolates the test. (Regression guard: an
    # earlier version wrote placeholder allowlists straight to the live file.)
    db.ACCESS_FILE = Path(tempfile.mkdtemp(prefix="sutando-ack-dtest-")) / "access.json"
    db._NOT_ALLOWLISTED_ACK_STATE_FILE = db.ACCESS_FILE.parent / "ack-state.json"
    db.STATE_DIR = db.ACCESS_FILE.parent
    db._not_allowlisted_ack_at.clear()
    ch = _FakeChannel()
    asyncio.run(db._ack_not_allowlisted(ch, "U_A", "alice"))
    check("discord: addressed non-allowlisted sender gets one ack", len(ch.sent) == 1, str(ch.sent))
    check("discord: ack text names the allowlist", "allowlist" in (ch.sent[0] if ch.sent else ""))
    asyncio.run(db._ack_not_allowlisted(ch, "U_A", "alice"))
    check("discord: repeat in the same channel is rate-limited", len(ch.sent) == 1, str(ch.sent))
    asyncio.run(db._ack_not_allowlisted(ch, "U_B", "bob"))
    check("discord: a different sender gets their own notice", len(ch.sent) == 2, str(ch.sent))

    # Simulate a bridge restart: clearing memory must not reset the durable
    # per-sender cooldown. A different channel still receives its own first notice.
    db._not_allowlisted_ack_at.clear()
    asyncio.run(db._ack_not_allowlisted(ch, "U_A", "alice"))
    check("discord: durable cooldown survives bridge restart", len(ch.sent) == 2, str(ch.sent))
    other = _FakeChannel(channel_id=456)
    asyncio.run(db._ack_not_allowlisted(other, "U_C", "carol"))
    check("discord: another channel receives its own notice", len(other.sent) == 1, str(other.sent))

    # Concurrent Discord event handlers must not both pass the cooldown check
    # before either send records its reservation. The same serialization also
    # prevents different-channel writers from overwriting each other's state.
    concurrent_root = Path(tempfile.mkdtemp(prefix="sutando-ack-concurrent-"))
    db.STATE_DIR = concurrent_root
    db._NOT_ALLOWLISTED_ACK_STATE_FILE = concurrent_root / "ack-state.json"
    db._not_allowlisted_ack_at.clear()
    same = _YieldingChannel(channel_id=600)
    asyncio.run(_run_concurrently(
        db._ack_not_allowlisted(same, "U_G", "gina"),
        db._ack_not_allowlisted(same, "U_G", "gina"),
    ))
    check("discord: concurrent handlers send one notice per key", len(same.sent) == 1, str(same.sent))
    asyncio.run(_run_concurrently(
        db._ack_not_allowlisted(same, "U_H", "hank"),
        db._ack_not_allowlisted(same, "U_K", "kira"),
    ))
    check("discord: concurrent distinct senders each get a notice", len(same.sent) == 3, str(same.sent))
    db._not_allowlisted_ack_at.clear()
    left = _YieldingChannel(channel_id=601)
    right = _YieldingChannel(channel_id=602)
    asyncio.run(_run_concurrently(
        db._ack_not_allowlisted(left, "U_I", "ivy"),
        db._ack_not_allowlisted(right, "U_J", "jules"),
    ))
    concurrent_state = db._not_allowlisted_ack_state()
    check(
        "discord: concurrent different-channel sends preserve both durable records",
        {"601:U_I", "602:U_J"}.issubset(concurrent_state),
        str(concurrent_state),
    )

    # An entry older than seven days is expired and replaced by a fresh send.
    db._NOT_ALLOWLISTED_ACK_STATE_FILE.write_text(json.dumps({
        "schema_version": 2,
        "sent_at_by_key": {
            f"{ch.id}:U_D": db.time.time() - db._NOT_ALLOWLISTED_ACK_COOLDOWN_S - 1,
        },
    }))
    db._not_allowlisted_ack_at.clear()
    asyncio.run(db._ack_not_allowlisted(ch, "U_D", "dana"))
    check("discord: sender notice is eligible again after seven days", len(ch.sent) == 3, str(ch.sent))

    # Corrupt/unwritable state must fail open: the sender still gets the notice
    # and an inability to persist the cooldown never breaks message handling.
    corrupt = _FakeChannel(channel_id=789)
    db._NOT_ALLOWLISTED_ACK_STATE_FILE.write_text("{")
    asyncio.run(db._ack_not_allowlisted(corrupt, "U_E", "erin"))
    check("discord: malformed cooldown state fails open", len(corrupt.sent) == 1, str(corrupt.sent))
    non_object = _FakeChannel(channel_id=791)
    db._NOT_ALLOWLISTED_ACK_STATE_FILE.write_text("[]")
    asyncio.run(db._ack_not_allowlisted(non_object, "U_G", "grace"))
    check(
        "discord: non-object cooldown state fails open",
        len(non_object.sent) == 1,
        str(non_object.sent),
    )
    blocked_parent = db.ACCESS_FILE.parent / "not-a-directory"
    blocked_parent.write_text("fixture")
    db._NOT_ALLOWLISTED_ACK_STATE_FILE = blocked_parent / "ack-state.json"
    db.STATE_DIR = blocked_parent
    unwritable = _FakeChannel(channel_id=790)
    try:
        asyncio.run(db._ack_not_allowlisted(unwritable, "U_F", "frank"))
        check("discord: state write failure does not block the notice", len(unwritable.sent) == 1, str(unwritable.sent))
    except Exception as e:
        check("discord: state write failure does not block the notice", False, repr(e))
    db.STATE_DIR = db.ACCESS_FILE.parent
    db._NOT_ALLOWLISTED_ACK_STATE_FILE = db.STATE_DIR / "ack-state.json"

    # Integration: drive the real _handle_discord_message DM path so a
    # non-allowlisted DM reaches the drop AND fires the ack (covers the wiring).
    import discord as _dsm

    class _FakeUser:
        def __init__(self, uid, bot=False): self.id = uid; self.bot = bot
        def __str__(self): return f"user{self.id}"
        def __eq__(self, o): return isinstance(o, _FakeUser) and o.id == self.id
        def __hash__(self): return hash(self.id)

    class _FakeDM(_dsm.DMChannel):
        def __init__(self, cid):
            self.id = cid; self.sent = []; self.references = []
        async def send(self, text, **kw):
            self.sent.append(text); self.references.append(kw.get("reference"))

    class _FakeMsg:
        def __init__(self, author, channel, content):
            self.author = author; self.channel = channel; self.content = content
            self.mentions = []; self.role_mentions = []; self.id = 12345
            self.type = _dsm.MessageType.default; self.reference = None
            self.embeds = []; self.guild = None; self.message_snapshots = []

    async def _noop(*a, **k): return None
    db._observe_for_mod = _noop            # unrelated mod hook
    db._update_dm_checkpoint = lambda *a, **k: None
    db.client.user = _FakeUser("BOT", bot=True)
    db._not_allowlisted_ack_at.clear()
    db.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    db.ACCESS_FILE.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["U_OWNER"]}))
    dm = _FakeDM(999)
    dm_msg = _FakeMsg(_FakeUser("U_STRANGER"), dm, "hi")
    asyncio.run(db._handle_discord_message(dm_msg))
    check("discord: _handle_discord_message DM drop fires the ack", len(dm.sent) == 1, str(dm.sent))
    dm_ref = dm.references[0] if dm.references else None
    check(
        "discord: DM ack replies to the triggering message without a hard anchor",
        getattr(dm_ref, "message_id", None) == dm_msg.id
        and getattr(dm_ref, "channel_id", None) == dm.id
        and getattr(dm_ref, "fail_if_not_exists", True) is False,
        str(dm_ref),
    )

    # Channel @mention drops → ack (covers the two channel wiring points).
    class _FakeGuild:
        def __init__(self, gid): self.id = gid
        def get_member(self, uid): return None

    class _FakeChan:
        def __init__(self, cid, name="general"):
            self.id = cid; self.name = name; self.sent = []; self.references = []
        async def send(self, text, **kw):
            self.sent.append(text); self.references.append(kw.get("reference"))

    def _chan_msg(uid, chan, guild):
        m = _FakeMsg(_FakeUser(uid), chan, "hey @bot")
        m.guild = guild
        m.mentions = [db.client.user]  # @mention → bot_mentioned
        return m

    g = _FakeGuild(1)
    # (a) unconfigured channel, sender not in global allowlist, @mentioned → ack
    db._not_allowlisted_ack_at.clear()
    db.ACCESS_FILE.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["U_OWNER"]}))
    c1 = _FakeChan(555)
    c1_msg = _chan_msg("U_S1", c1, g)
    asyncio.run(db._handle_discord_message(c1_msg))
    check("discord: channel @mention, unconfigured + not in global allowlist → ack", len(c1.sent) == 1, str(c1.sent))
    c1_ref = c1.references[0] if c1.references else None
    check(
        "discord: channel ack uses a non-failing reply reference",
        getattr(c1_ref, "message_id", None) == c1_msg.id
        and getattr(c1_ref, "channel_id", None) == c1.id
        and getattr(c1_ref, "fail_if_not_exists", True) is False,
        str(c1_ref),
    )
    # (b) configured channel whose allowFrom excludes the sender, @mentioned → ack
    db._not_allowlisted_ack_at.clear()
    db.ACCESS_FILE.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["U_OWNER"],
        "groups": {"777": {"requireMention": True, "allowFrom": ["U_OWNER"]}}}))
    c2 = _FakeChan(777)
    asyncio.run(db._handle_discord_message(_chan_msg("U_S2", c2, g)))
    check("discord: channel @mention, channel allowlist excludes sender → ack", len(c2.sent) == 1, str(c2.sent))

    # send failure is swallowed (covers the except branch)
    class _BoomChan:
        id = 888
        async def send(self, *a, **k): raise RuntimeError("boom")
    db._not_allowlisted_ack_at.clear()
    try:
        asyncio.run(db._ack_not_allowlisted(_BoomChan(), "U_BOOM", "boom"))
        check("discord: a send failure in the ack is swallowed (no raise)", True)
    except Exception as e:
        check("discord: a send failure in the ack is swallowed (no raise)", False, repr(e))

    # Discord can reject reply anchors for deleted/system messages. Retry once
    # without a reference so the allowlist notice is not dropped entirely.
    class _FakeHTTPException(Exception):
        pass

    class _RejectedAnchorChan:
        id = 889
        def __init__(self):
            self.references = []
            self.sent = []
        async def send(self, text, **kw):
            ref = kw.get("reference")
            self.references.append(ref)
            if ref is not None:
                raise _FakeHTTPException("invalid message reference")
            self.sent.append(text)

    original_http_exception = db.discord.HTTPException
    db.discord.HTTPException = _FakeHTTPException
    fallback = _RejectedAnchorChan()
    fallback_message = _FakeMsg(_FakeUser("U_FALLBACK"), fallback, "hi")
    db._not_allowlisted_ack_at.clear()
    try:
        asyncio.run(db._ack_not_allowlisted(
            fallback, "U_FALLBACK", "fallback", fallback_message
        ))
    finally:
        db.discord.HTTPException = original_http_exception
    check(
        "discord: rejected reply anchor retries as a fresh notice",
        fallback.sent == [db._NOT_ALLOWLISTED_ACK_TEXT]
        and len(fallback.references) == 2
        and fallback.references[0] is not None
        and fallback.references[1] is None,
        f"sent={fallback.sent!r} refs={fallback.references!r}",
    )


# ─────────────────────────── Slack ───────────────────────────
def _load_slack():
    # Slack also resolves its channel paths during import, so seed a canonical
    # temp access file before loading the module. This prevents the legacy
    # reader fallback from consulting or mutating the developer's live config.
    ccd = Path(tempfile.mkdtemp(prefix="sutando-ack-slack-ccd-"))
    channel_dir = ccd / "channels" / "slack"
    channel_dir.mkdir(parents=True)
    (channel_dir / "access.json").write_text("{}\n")
    os.environ["CLAUDE_CONFIG_DIR"] = str(ccd)
    class _FakeClient:
        def __init__(self): self.posts = []
        def chat_postMessage(self, **kw): self.posts.append(kw); return {"ok": True}
    class _FakeApp:
        def __init__(self, *a, **k): self.client = _FakeClient()
        def _dec(self, *a, **k): return lambda fn: fn
        event = message = command = action = _dec
    bolt = types.ModuleType("slack_bolt"); bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    ad = types.ModuleType("slack_bolt.adapter"); sm = types.ModuleType("slack_bolt.adapter.socket_mode")
    sm.SocketModeHandler = type("SMH", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["slack_bolt.adapter"] = ad; sys.modules["slack_bolt.adapter.socket_mode"] = sm
    tmp = tempfile.mkdtemp(prefix="sutando-ack-test-")
    os.environ["SUTANDO_WORKSPACE"] = tmp
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"; os.environ["SLACK_APP_TOKEN"] = "xapp-test"
    spec = importlib.util.spec_from_file_location("sbridge_ack", REPO / "src" / "slack-bridge.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert Path(m.channels_env).parent == channel_dir
    assert Path(m.ACCESS_FILE) == channel_dir / "access.json"
    return m


def test_slack():
    sb = _load_slack()
    # CRITICAL: isolate ACCESS_FILE from the real slack access.json (see test_discord).
    sb.ACCESS_FILE = Path(tempfile.mkdtemp(prefix="sutando-ack-stest-")) / "access.json"
    sb._not_allowlisted_ack_at.clear()
    dm_event = {"channel": "D123", "channel_type": "im", "ts": "1.1"}
    sb._ack_not_allowlisted(dm_event, "U_A")
    posts = sb.app.client.posts
    check("slack: addressed non-allowlisted sender gets one ack", len(posts) == 1, str(posts))
    check("slack: DM ack posts top-level (no thread_ts)", posts and posts[0].get("thread_ts") is None)
    sb._ack_not_allowlisted(dm_event, "U_A")
    check("slack: repeat from same sender is rate-limited", len(posts) == 1, str(posts))
    mention_event = {"channel": "C999", "channel_type": "channel", "ts": "2.2", "thread_ts": "2.0"}
    sb._ack_not_allowlisted(mention_event, "U_B")
    check("slack: a different sender is not rate-limited", len(posts) == 2, str(posts))
    check("slack: channel @mention ack replies in-thread", posts[-1].get("thread_ts") == "2.0", str(posts[-1]))

    # End-to-end drop path: _write_task for a non-allowlisted user acks + writes no task.
    sb._not_allowlisted_ack_at.clear(); sb.app.client.posts.clear()
    # Configure the (temp) access.json with an allowlist that EXCLUDES our sender.
    try:
        access_path = sb.ACCESS_FILE
        access_path.parent.mkdir(parents=True, exist_ok=True)
        access_path.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["U_OWNER"], "tierMap": {"U_OWNER": "owner"}}))
        ev = {"user": "U_STRANGER", "channel": "D1", "channel_type": "im", "ts": "3.3", "text": "hi"}
        tid = sb._write_task(ev, "Slack DM", "hi", "stranger")
        check("slack: _write_task drops the non-allowlisted sender (returns None)", tid is None, str(tid))
        check("slack: _write_task fired the ack on drop", len(sb.app.client.posts) == 1, str(sb.app.client.posts))
    except Exception as e:
        check("slack: _write_task drop-path exercised", False, repr(e))

    # send failure is swallowed (covers the slack except branch)
    def _boom(**kw): raise RuntimeError("boom")
    sb.app.client.chat_postMessage = _boom
    sb._not_allowlisted_ack_at.clear()
    try:
        sb._ack_not_allowlisted({"channel": "D1", "channel_type": "im", "ts": "9.9"}, "U_BOOM")
        check("slack: a send failure in the ack is swallowed (no raise)", True)
    except Exception as e:
        check("slack: a send failure in the ack is swallowed (no raise)", False, repr(e))


if __name__ == "__main__":
    test_discord()
    test_slack()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): {FAILS}")
        sys.exit(1)
    print("\nPASS — not-allowlisted ack behavioral tests (discord + slack)")
