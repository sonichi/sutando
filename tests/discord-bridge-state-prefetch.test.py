#!/usr/bin/env python3
"""
Tests for the Discord-state-reference pre-fetch path in `discord-bridge.py`.

Per Chi's 2026-05-08 strategy chat (option 3 systemic fix): when a non-owner
task body references Discord-side state via `<#channel_id>`, the bridge
attempts to fetch the referenced channel's recent messages via the bot's REST
client and PREPEND them to the task body BEFORE falling through to the
PR #639 silent-escalate path. The agent then has the data inline and can
answer normally without codex sandbox needing API access.

Cases:
  Pure pre-fetch — `_prefetch_discord_state_refs`:
    a) ref + readable channel with messages → prepend formatted block
    b) ref + history() raises Forbidden → returns None (caller falls through)
    c) ref + channel not found (None from get_channel + fetch_channel) → None
    d) ref + wrong channel type (voice/category) → None
    e) multiple refs → all prepended in source-order
    f) duplicate refs in same body → fetched once, prepended once
    g) empty channel (history() yields nothing) → empty marker; not added
    h) cache hit within TTL → second call doesn't re-fetch
    i) no refs → returns None (caller proceeds normally without prepend)
    j) unexpected exception inside fetch → silent fail, returns None

Run: python3 tests/discord-bridge-state-prefetch.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub minimal discord module — same pattern as discord-state-detection test.
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
    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise _NotFound("channel not found")
        return ch


class _AllowedMentions:
    def __init__(self, *a, **kw): pass


class _DMChannel: pass


class _DiscordException(Exception): pass
class _HTTPException(_DiscordException): pass
class _NotFound(_HTTPException): pass
class _Forbidden(_HTTPException): pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.AllowedMentions = _AllowedMentions
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.DiscordException = _DiscordException
_discord_stub.HTTPException = _HTTPException
_discord_stub.NotFound = _NotFound
_discord_stub.Forbidden = _Forbidden
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

class _MockMessage:
    """Minimal Discord Message mock for history() iteration."""
    def __init__(self, mid, author_name, content, created_at):
        self.id = mid
        self.author = types.SimpleNamespace(name=author_name)
        self.content = content
        self.created_at = created_at


class _MockHistoryIterator:
    """Async iterator mimicking discord.py's HistoryIterator."""
    def __init__(self, messages, raise_exc=None):
        self._messages = list(messages)
        self._raise = raise_exc
        self._idx = 0
    def __aiter__(self): return self
    async def __anext__(self):
        if self._raise is not None:
            raise self._raise
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        m = self._messages[self._idx]
        self._idx += 1
        return m


class _MockChannel:
    def __init__(self, channel_id, name="ch", ch_type="text", messages=None, history_raises=None):
        self.id = channel_id
        self.name = name
        self.type = ch_type
        self._messages = messages or []
        self._history_raises = history_raises
        self.history_call_count = 0
    def history(self, limit=5):
        self.history_call_count += 1
        return _MockHistoryIterator(self._messages[:limit], raise_exc=self._history_raises)


def _install_mock_client(mock_channels_by_id):
    c = _Client()
    c._channels = mock_channels_by_id
    bridge.client = c


def _clear_cache():
    if hasattr(bridge, "_PREFETCH_CACHE"):
        bridge._PREFETCH_CACHE.clear()


def _now_dt():
    """Return a datetime for mock-message timestamps."""
    import datetime
    return datetime.datetime(2026, 5, 9, 8, 0, 0)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_a_ref_with_perms_prepends():
    """Channel readable + messages → enriched body has channel block + original."""
    fails = []
    _clear_cache()
    msgs = [
        _MockMessage("1", "snoopz_loc", "self-promo content", _now_dt()),
        _MockMessage("2", "elcapitan__", "hackathon post", _now_dt()),
    ]
    ch = _MockChannel(1153072414184452241, "general", "text", msgs)
    _install_mock_client({1153072414184452241: ch})
    body = "classify the latest message in <#1153072414184452241>"
    enriched = asyncio.run(bridge._prefetch_discord_state_refs(body))
    if enriched is None:
        fails.append("a) expected enriched body, got None")
        return fails
    if "Channel <#1153072414184452241> recent messages" not in enriched:
        fails.append("a) enriched should contain channel block header")
    if "snoopz_loc" not in enriched or "self-promo content" not in enriched:
        fails.append("a) enriched should include fetched message content + author")
    if "[Original task body:]" not in enriched:
        fails.append("a) enriched should include separator before original")
    if body not in enriched:
        fails.append("a) enriched should preserve original body")
    return fails


def case_b_history_forbidden_falls_through():
    """history() raises Forbidden → returns None (caller silent-escalates)."""
    fails = []
    _clear_cache()
    ch = _MockChannel(1153072414184452241, "general", "text", history_raises=_Forbidden("no access"))
    _install_mock_client({1153072414184452241: ch})
    enriched = asyncio.run(bridge._prefetch_discord_state_refs("classify <#1153072414184452241>"))
    if enriched is not None:
        fails.append(f"b) Forbidden should return None, got {type(enriched).__name__}")
    return fails


def case_c_channel_not_found_falls_through():
    """get_channel + fetch_channel both fail → returns None."""
    fails = []
    _clear_cache()
    _install_mock_client({})  # empty: get_channel returns None, fetch_channel raises NotFound
    enriched = asyncio.run(bridge._prefetch_discord_state_refs("classify <#9999999999>"))
    if enriched is not None:
        fails.append(f"c) channel-not-found should return None, got {type(enriched).__name__}")
    return fails


def case_d_wrong_channel_type_falls_through():
    """Voice/category channel type → returns None."""
    fails = []
    _clear_cache()
    ch = _MockChannel(1234, "voice-room", "voice", [])
    _install_mock_client({1234: ch})
    enriched = asyncio.run(bridge._prefetch_discord_state_refs("classify <#1234>"))
    if enriched is not None:
        fails.append(f"d) voice channel should return None, got {type(enriched).__name__}")
    return fails


def case_e_multiple_refs_all_prepended_in_order():
    """Two refs in body → both fetched, both prepended in source-order."""
    fails = []
    _clear_cache()
    ch1 = _MockChannel(111, "ch-one", "text", [_MockMessage("a", "u1", "msg from ch1", _now_dt())])
    ch2 = _MockChannel(222, "ch-two", "text", [_MockMessage("b", "u2", "msg from ch2", _now_dt())])
    _install_mock_client({111: ch1, 222: ch2})
    body = "compare <#111> with <#222>"
    enriched = asyncio.run(bridge._prefetch_discord_state_refs(body))
    if enriched is None:
        fails.append("e) expected enriched body, got None")
        return fails
    pos1 = enriched.find("Channel <#111>")
    pos2 = enriched.find("Channel <#222>")
    if pos1 == -1 or pos2 == -1:
        fails.append(f"e) both channel blocks expected; pos1={pos1} pos2={pos2}")
    elif pos1 >= pos2:
        fails.append(f"e) source-order expected: <#111> before <#222>; pos1={pos1} pos2={pos2}")
    return fails


def case_f_duplicate_refs_dedup():
    """Same ref twice in body → channel fetched once, block appears once."""
    fails = []
    _clear_cache()
    ch = _MockChannel(555, "ch", "text", [_MockMessage("a", "u1", "msg", _now_dt())])
    _install_mock_client({555: ch})
    body = "first look at <#555>, then re-check <#555>"
    enriched = asyncio.run(bridge._prefetch_discord_state_refs(body))
    if enriched is None:
        fails.append("f) expected enriched body, got None")
        return fails
    occurrences = enriched.count("Channel <#555> recent messages")
    if occurrences != 1:
        fails.append(f"f) expected 1 channel block, got {occurrences}")
    if ch.history_call_count > 1:
        fails.append(f"f) duplicate refs should fetch once; got {ch.history_call_count} calls")
    return fails


def case_g_empty_channel_renders_marker():
    """Channel readable but no messages → empty-string from fetch; rendered as
    `[no recent messages]` block in enriched body. Empty IS a real answer
    (per PR #644 v2 review fix #2 — distinguish empty from failed fetch)."""
    fails = []
    _clear_cache()
    ch = _MockChannel(666, "empty", "text", messages=[])
    _install_mock_client({666: ch})
    enriched = asyncio.run(bridge._prefetch_discord_state_refs("look at <#666>"))
    if enriched is None:
        fails.append("g) empty channel should yield enriched body with 'no recent messages' marker; got None")
        return fails
    if "no recent messages" not in enriched:
        fails.append(f"g) enriched body should contain 'no recent messages' marker; got: {enriched[:200]!r}")
    if "Channel <#666>" not in enriched:
        fails.append(f"g) enriched body should reference <#666>; got: {enriched[:200]!r}")
    return fails


def case_h_cache_hit_skips_refetch():
    """Same ref fetched twice within TTL → second call is cache hit."""
    fails = []
    _clear_cache()
    ch = _MockChannel(777, "ch", "text", [_MockMessage("a", "u1", "msg", _now_dt())])
    _install_mock_client({777: ch})
    asyncio.run(bridge._prefetch_discord_state_refs("read <#777>"))
    first_count = ch.history_call_count
    if first_count != 1:
        fails.append(f"h) expected 1 fetch on first call, got {first_count}")
    asyncio.run(bridge._prefetch_discord_state_refs("re-read <#777>"))
    if ch.history_call_count != first_count:
        fails.append(f"h) cache hit expected; got {ch.history_call_count} fetches (first was {first_count})")
    return fails


def case_i_no_refs_returns_none():
    """Body with no `<#...>` → returns None (caller proceeds without enrichment)."""
    fails = []
    _clear_cache()
    _install_mock_client({})
    enriched = asyncio.run(bridge._prefetch_discord_state_refs("just a plain question, no refs"))
    if enriched is not None:
        fails.append(f"i) no-refs body should return None, got {type(enriched).__name__}")
    enriched_empty = asyncio.run(bridge._prefetch_discord_state_refs(""))
    if enriched_empty is not None:
        fails.append(f"i2) empty body should return None")
    enriched_none = asyncio.run(bridge._prefetch_discord_state_refs(None))
    if enriched_none is not None:
        fails.append(f"i3) None body should return None")
    return fails


def case_j_unexpected_exception_silent_fail():
    """Any unexpected exception in the fetch path → silent fail, returns None."""
    fails = []
    _clear_cache()
    ch = _MockChannel(888, "ch", "text", history_raises=RuntimeError("internal error"))
    _install_mock_client({888: ch})
    # Should not raise — should fail silently and return None
    try:
        enriched = asyncio.run(bridge._prefetch_discord_state_refs("read <#888>"))
    except Exception as e:
        fails.append(f"j) unexpected exception leaked out of prefetch: {type(e).__name__}: {e}")
        return fails
    if enriched is not None:
        fails.append(f"j) unexpected exception should return None, got enriched body")
    return fails


def case_k_partial_failure_fails_whole_prefetch():
    """v2 fix #1: multi-ref task where one ref succeeds and one fails should
    fail the whole prefetch (return None), not return partial context. Lets
    silent-escalate handle the whole task instead of letting the agent see
    only half the references and answer confidently-wrong."""
    fails = []
    _clear_cache()
    good = _MockChannel(111, "good", "text", [_MockMessage("a", "u1", "msg from good", _now_dt())])
    bad = _MockChannel(222, "bad", "text", history_raises=_Forbidden("no access"))
    _install_mock_client({111: good, 222: bad})
    body = "compare <#111> with <#222>"
    enriched = asyncio.run(bridge._prefetch_discord_state_refs(body))
    if enriched is not None:
        fails.append("k) one-ref-fails should fail whole prefetch (return None); got partial enriched body")
        if "Channel <#111>" in (enriched or "") and "Channel <#222>" not in (enriched or ""):
            fails.append("k)   ...and partial body contains good ref but not bad — exactly the wrong-answer pattern PR #644 v2 prevents")
    return fails


def case_l_history_timeout_returns_none():
    """v2 fix #3: a history() call that hangs longer than the per-ref timeout
    should be treated as a failed fetch. Simulated by patching
    _PREFETCH_PER_REF_TIMEOUT_S to a small value and an async history that
    sleeps longer."""
    fails = []
    _clear_cache()
    import asyncio as _asyncio

    class _SlowHistory:
        def __init__(self, delay_s):
            self._delay = delay_s
        def __aiter__(self): return self
        async def __anext__(self):
            await _asyncio.sleep(self._delay)
            raise StopAsyncIteration

    class _SlowChannel:
        def __init__(self, channel_id, delay_s):
            self.id = channel_id
            self.type = "text"
            self.name = "slow"
            self._delay = delay_s
            self.history_call_count = 0
        def history(self, limit=5):
            self.history_call_count += 1
            return _SlowHistory(self._delay)

    # Bridge will use whatever _PREFETCH_PER_REF_TIMEOUT_S is at call time.
    original_timeout = bridge._PREFETCH_PER_REF_TIMEOUT_S
    try:
        bridge._PREFETCH_PER_REF_TIMEOUT_S = 0.05  # 50ms
        slow = _SlowChannel(999, delay_s=0.5)  # 500ms — well over the timeout
        _install_mock_client({999: slow})
        enriched = asyncio.run(bridge._prefetch_discord_state_refs("read <#999>"))
        if enriched is not None:
            fails.append("l) history timeout should fail prefetch (return None); got enriched body")
    finally:
        bridge._PREFETCH_PER_REF_TIMEOUT_S = original_timeout
    return fails


def main():
    cases = [
        ("a-ref-with-perms-prepends", case_a_ref_with_perms_prepends),
        ("b-history-forbidden-falls-through", case_b_history_forbidden_falls_through),
        ("c-channel-not-found-falls-through", case_c_channel_not_found_falls_through),
        ("d-wrong-channel-type-falls-through", case_d_wrong_channel_type_falls_through),
        ("e-multiple-refs-source-order", case_e_multiple_refs_all_prepended_in_order),
        ("f-duplicate-refs-dedup", case_f_duplicate_refs_dedup),
        ("g-empty-channel-renders-marker", case_g_empty_channel_renders_marker),
        ("h-cache-hit-skips-refetch", case_h_cache_hit_skips_refetch),
        ("i-no-refs-returns-none", case_i_no_refs_returns_none),
        ("j-unexpected-exception-silent-fail", case_j_unexpected_exception_silent_fail),
        ("k-partial-failure-fails-whole-prefetch", case_k_partial_failure_fails_whole_prefetch),
        ("l-history-timeout-returns-none", case_l_history_timeout_returns_none),
    ]
    failures = []
    for label, fn in cases:
        fs = fn()
        if fs:
            failures.extend([(label, m) for m in fs])
            print(f"  ✗ case {label}")
            for f in fs:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll Discord-state pre-fetch invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
