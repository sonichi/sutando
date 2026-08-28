#!/usr/bin/env python3
"""Reconnect backfill for served guild channels (discord-bridge).

## The gap

A Discord gateway disconnect that outlasts the RESUME window forces a full
IDENTIFY reconnect, and IDENTIFY does NOT replay `MESSAGE_CREATE` events that
arrived during the gap. DMs already have a REST catch-up
(`_catchup_missed_dms` + `state/discord-dm-checkpoint.json`); guild channels
and threads the bridge serves had none — a channel message sent during the
gap was silently lost.

## The fix under test

Per-channel watermark in `state/discord-last-seen.json` for every channel in
access.json `groups`, advanced as live messages are observed. On READY and
RESUMED, `_backfill_missed_channel_messages()` REST-fetches
`after=<watermark>` (oldest-first, capped at
CHANNEL_BACKFILL_MAX_MESSAGES per channel) and routes each missed message
through `_handle_discord_message` — the same choke point live messages take,
so every access gate applies identically. A channel with no watermark only
initializes it to the newest id (fresh installs never ingest history).

All tests drive the production functions; the Discord client and channels are
faked at the API boundary only.
"""

import asyncio
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import types
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Set workspace BEFORE importing the bridge — it captures state-file
# paths at module-load time.
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-reconnect-backfill-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
(Path(_WORKSPACE_TMP) / "state").mkdir(parents=True, exist_ok=True)


def _load(name: str, path: Path):
    if "discord" not in sys.modules:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        stub.Object = lambda id: type("Object", (), {"id": id})()
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("discord_bridge_backfill", REPO / "src" / "discord-bridge.py")

# Redirect access control to a test-owned file: the backfill's channel set and
# the live watermark gate both read ACCESS_FILE.
ACCESS_TMP = Path(_WORKSPACE_TMP) / "access.json"
bridge.ACCESS_FILE = ACCESS_TMP

# Valid Discord snowflake shapes (17-20 digits) — _backfill_channel_ids
# filters on shape.
CH1 = "223456789012345678"
CH2 = "323456789012345678"
UNTRACKED = "423456789012345678"


def _set_groups(*channel_ids):
    ACCESS_TMP.write_text(json.dumps({
        "allowFrom": [],
        "groups": {cid: {"requireMention": False} for cid in channel_ids},
    }))


def _clear_watermarks():
    f = bridge.LAST_SEEN_FILE
    if f.exists():
        f.unlink()


def _msg(mid, author):
    return types.SimpleNamespace(id=mid, author=author)


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeChannel:
    """Fakes only the `history()` API edge; everything else is production."""

    def __init__(self, cid, messages=(), fail_history=False):
        self.id = cid
        self.history_calls = []
        self._messages = list(messages)
        self._fail = fail_history

    def history(self, *, limit=None, after=None, oldest_first=False):
        if self._fail:
            raise RuntimeError("history exploded")
        self.history_calls.append(
            {"limit": limit, "after": after, "oldest_first": oldest_first})
        msgs = list(self._messages)
        if after is not None:
            msgs = [m for m in msgs if m.id > after.id]
        msgs.sort(key=lambda m: m.id, reverse=not oldest_first)
        if limit is not None:
            msgs = msgs[:limit]
        return _AsyncIter(msgs)


class _FakeClient:
    def __init__(self, channels, user):
        self._channels = {int(c.id): c for c in channels}
        self.user = user

    def get_channel(self, cid):
        return self._channels.get(int(cid))

    async def fetch_channel(self, cid):
        ch = self._channels.get(int(cid))
        if ch is None:
            raise RuntimeError(f"unknown channel {cid}")
        return ch


class _HandlerSpy:
    """Stands in for the module attribute `_handle_discord_message`, so a call
    through it proves the backfill dispatches to the live choke-point NAME —
    a private copy of the handler would not route through this."""

    def __init__(self):
        self.calls = []

    async def __call__(self, message, force=False):
        self.calls.append(message)


@contextmanager
def _patched(client, handler):
    real_client = bridge.client
    real_handler = bridge._handle_discord_message
    bridge.client = client
    bridge._handle_discord_message = handler
    try:
        yield
    finally:
        bridge.client = real_client
        bridge._handle_discord_message = real_handler


def test_first_ready_initializes_watermarks_without_replay():
    """(i) No watermark yet → pin to the channel's newest id via one
    limit=1 fetch; nothing is replayed. A fresh install must never ingest
    channel history as tasks."""
    _clear_watermarks()
    _set_groups(CH1)
    me, other = object(), object()
    ch = _FakeChannel(int(CH1), [_msg(100, other), _msg(300, other), _msg(200, other)])
    spy = _HandlerSpy()
    with _patched(_FakeClient([ch], me), spy):
        asyncio.run(bridge._backfill_missed_channel_messages())
    assert spy.calls == [], (
        f"first READY must not replay history; handler got {[m.id for m in spy.calls]}")
    assert bridge._load_last_seen().get(CH1) == "300", (
        f"watermark should pin to newest id 300, got {bridge._load_last_seen()!r}")
    assert len(ch.history_calls) == 1, ch.history_calls
    call = ch.history_calls[0]
    assert call["limit"] == 1 and call["after"] is None, (
        f"init must be one cheap limit=1 fetch, got {call!r}")


def test_reconnect_replays_after_watermark_through_live_handler():
    """(ii) With a watermark, the backfill fetches after=<watermark>
    oldest-first and each fetched message goes through the SAME
    `_handle_discord_message` the live gateway path calls (the spy replaces
    that module attribute — a copied handler would bypass it). Self-authored
    messages are skipped but still advance the watermark."""
    _clear_watermarks()
    _set_groups(CH1)
    me, other = object(), object()
    bridge._update_last_seen(CH1, 100)
    ch = _FakeChannel(int(CH1), [
        _msg(100, other),   # at the watermark — must not be re-fetched
        _msg(150, other),
        _msg(200, me),      # bot's own message — skipped
        _msg(250, other),
    ])
    spy = _HandlerSpy()
    with _patched(_FakeClient([ch], me), spy):
        asyncio.run(bridge._backfill_missed_channel_messages())
    assert [m.id for m in spy.calls] == [150, 250], (
        f"expected [150, 250] through the choke point, got {[m.id for m in spy.calls]}")
    call = ch.history_calls[0]
    assert call["after"] is not None and call["after"].id == 100, call
    assert call["oldest_first"] is True, call
    assert bridge._load_last_seen().get(CH1) == "250", bridge._load_last_seen()


def test_backfill_routes_through_the_chokepoint_source_pin():
    """Structural half of (ii): the backfill body awaits the exact name the
    live `on_message` event awaits, so a refactor that swaps in a copy of the
    handler fails loudly."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    assert "await _handle_discord_message(message)" in src, (
        "live path no longer routes through _handle_discord_message")
    block = re.search(
        r"async def _backfill_one_channel\(.*?\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL)
    assert block, "could not locate _backfill_one_channel"
    assert "await _handle_discord_message(msg)" in block.group(1), (
        "_backfill_one_channel must dispatch through _handle_discord_message, "
        "not a private copy of the handling logic")


def test_backfill_cap_binds_and_warns():
    """(iii) More than CHANNEL_BACKFILL_MAX_MESSAGES missed → only the oldest
    cap-many are replayed and a WARN names the cap; the watermark stops at the
    last replayed id so the rest are fetched next reconnect."""
    _clear_watermarks()
    _set_groups(CH1)
    me, other = object(), object()
    cap = bridge.CHANNEL_BACKFILL_MAX_MESSAGES
    bridge._update_last_seen(CH1, 1000)
    ch = _FakeChannel(int(CH1), [_msg(1000 + i, other) for i in range(1, cap + 11)])
    spy = _HandlerSpy()
    buf = io.StringIO()
    with _patched(_FakeClient([ch], me), spy), redirect_stdout(buf):
        asyncio.run(bridge._backfill_missed_channel_messages())
    out = buf.getvalue()
    assert len(spy.calls) == cap, (
        f"cap must bind at {cap}, handler got {len(spy.calls)} messages")
    assert [m.id for m in spy.calls] == [1000 + i for i in range(1, cap + 1)], (
        "cap must keep the OLDEST messages")
    assert "WARN" in out and str(cap) in out, (
        f"cap binding must log a WARN naming the cap; got: {out!r}")
    assert bridge._load_last_seen().get(CH1) == str(1000 + cap), (
        f"watermark must stop at the last replayed id, got {bridge._load_last_seen()!r}")


def test_watermark_advances_on_live_messages():
    """(iv) A live message observed in a served channel advances the
    watermark, driven end-to-end through the production handler (self-author
    branch — the same starvation class the DM checkpoint hit: our own replies
    dominate active channels and must advance the watermark too)."""
    _clear_watermarks()
    _set_groups(CH1)
    me = object()
    fake_client = type("_C", (), {"user": me})()
    msg = types.SimpleNamespace(
        author=me, channel=types.SimpleNamespace(id=int(CH1)), id=99999)
    real_client = bridge.client
    bridge.client = fake_client
    try:
        asyncio.run(bridge._handle_discord_message(msg))
    finally:
        bridge.client = real_client
    assert bridge._load_last_seen().get(CH1) == "99999", (
        f"live message must advance the served-channel watermark; got "
        f"{bridge._load_last_seen()!r}")


def test_untracked_channel_leaves_watermark_untouched():
    """Only channels in access.json `groups` are watermarked — the backfill
    only serves those, so tracking every visible channel would be dead state."""
    _clear_watermarks()
    _set_groups(CH1)
    me = object()
    fake_client = type("_C", (), {"user": me})()
    msg = types.SimpleNamespace(
        author=me, channel=types.SimpleNamespace(id=int(UNTRACKED)), id=99999)
    real_client = bridge.client
    bridge.client = fake_client
    try:
        asyncio.run(bridge._handle_discord_message(msg))
    finally:
        bridge.client = real_client
    assert bridge._load_last_seen() == {}, (
        f"unserved channel must not be watermarked; got {bridge._load_last_seen()!r}")


def test_live_advance_covers_all_observation_sites():
    """Structural: the handler advances the watermark at every early-return
    observation site (self-authored, system message) plus the main path —
    a frozen watermark replays the same window on every reconnect."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    block = re.search(
        r"async def _handle_discord_message\(.*?\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL)
    assert block, "could not locate _handle_discord_message"
    n = block.group(1).count("_note_channel_message_seen(message)")
    assert n >= 3, (
        f"expected the watermark advance at the self/system/main observation "
        f"sites (>=3), found {n}")


def test_channel_error_does_not_stop_other_channels():
    """(v) One channel erroring mid-backfill logs and moves on; the remaining
    channels still replay."""
    _clear_watermarks()
    _set_groups(CH1, CH2)
    me, other = object(), object()
    bridge._update_last_seen(CH1, 10)
    bridge._update_last_seen(CH2, 20)
    bad = _FakeChannel(int(CH1), fail_history=True)
    good = _FakeChannel(int(CH2), [_msg(25, other)])
    spy = _HandlerSpy()
    buf = io.StringIO()
    with _patched(_FakeClient([bad, good], me), spy), redirect_stdout(buf):
        asyncio.run(bridge._backfill_missed_channel_messages())
    assert [m.id for m in spy.calls] == [25], (
        f"the healthy channel must still backfill; handler got "
        f"{[m.id for m in spy.calls]}; log: {buf.getvalue()!r}")
    assert CH1 in buf.getvalue(), (
        f"the failing channel must be named in the log; got {buf.getvalue()!r}")


def test_last_seen_advances_forward_only():
    """The watermark shares the DM checkpoint's forward-only contract: an
    out-of-order replay can never move it backwards."""
    _clear_watermarks()
    bridge._update_last_seen(CH1, 200)
    bridge._update_last_seen(CH1, 150)
    assert bridge._load_last_seen().get(CH1) == "200", bridge._load_last_seen()


def test_source_wires_backfill_into_ready_and_resumed():
    """Architectural: the backfill must be scheduled from BOTH `on_ready`
    (IDENTIFY reconnects) and `on_resumed` (RESUME can be accepted after
    boundary drops). Without the wiring the watermark advances but nothing
    ever replays."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    ready = re.search(
        r"async def on_ready\(\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL)
    assert ready, "could not locate on_ready"
    assert "_backfill_missed_channel_messages" in ready.group(1) and \
        "create_task" in ready.group(1), (
        "on_ready does not schedule _backfill_missed_channel_messages")
    resumed = re.search(
        r"async def on_resumed\(\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL)
    assert resumed, "could not locate on_resumed"
    assert "_backfill_missed_channel_messages" in resumed.group(1), (
        "on_resumed does not schedule the channel backfill")
    assert "_catchup_missed_dms" in resumed.group(1), (
        "on_resumed does not schedule the DM catch-up alongside the channel "
        "backfill — DM coverage would be READY-only")


def main():
    failures = []
    for fn in (
        test_first_ready_initializes_watermarks_without_replay,
        test_reconnect_replays_after_watermark_through_live_handler,
        test_backfill_routes_through_the_chokepoint_source_pin,
        test_backfill_cap_binds_and_warns,
        test_watermark_advances_on_live_messages,
        test_untracked_channel_leaves_watermark_untouched,
        test_live_advance_covers_all_observation_sites,
        test_channel_error_does_not_stop_other_channels,
        test_last_seen_advances_forward_only,
        test_source_wires_backfill_into_ready_and_resumed,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All reconnect-backfill tests passed.")


if __name__ == "__main__":
    main()
