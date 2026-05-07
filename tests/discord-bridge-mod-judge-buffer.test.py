#!/usr/bin/env python3
"""
Tests for the auto-mod buffer + flush + on_message observation hook
in discord-bridge.py.

PR6 of 6 (final). Covers `_observe_for_mod` (the on_message hook) and
`_flush_mod_buffer` (the periodic drain). Patches `_codex_judge_batch`
and the action helpers to keep tests deterministic.

Run: python3 tests/discord-bridge-mod-judge-buffer.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import asyncio
import importlib.util
import json
import sys
import tempfile
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


# ---------------------------------------------------------------------------
# Helpers / mocks
# ---------------------------------------------------------------------------

def _make_message(msg_id, guild_id, author_id, channel_id=42, content="hi",
                  is_bot=False, role_ids=None):
    roles = [types.SimpleNamespace(id=r) for r in (role_ids or [])]
    author = types.SimpleNamespace(
        id=author_id, display_name=f"u{author_id}", bot=is_bot, roles=roles
    )
    guild = types.SimpleNamespace(id=guild_id, owner_id=None)
    channel = types.SimpleNamespace(id=channel_id, name="general")
    return types.SimpleNamespace(
        id=msg_id, author=author, guild=guild, channel=channel,
        content=content, reference=None,
    )


def _write_access(active=True, mod_roles=None, guild_id=42):
    """Write a temp access.json; return path."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"guilds": {str(guild_id): {
        "mod_active": active,
        "moderator_roles": mod_roles or [],
    }}}, f)
    f.close()
    return Path(f.name)


def _reset_buffer():
    """Tests share module-level _mod_buffer; clear between cases."""
    bridge._mod_buffer.clear()
    bridge._mod_buffer_lock = None


# ---------------------------------------------------------------------------
# _observe_for_mod
# ---------------------------------------------------------------------------

def case_observe_buffers_normal_message() -> list[str]:
    fails = []
    _reset_buffer()
    path = _write_access(active=True, mod_roles=["111"], guild_id=42)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        msg = _make_message("m1", 42, 999, content="hello")
        asyncio.run(bridge._observe_for_mod(msg))
        if len(bridge._mod_buffer) != 1:
            fails.append(f"a) buffer should hold 1 record, got {len(bridge._mod_buffer)}")
        if bridge._mod_buffer and bridge._mod_buffer[0]["msg_id"] != "m1":
            fails.append("a) record msg_id should be preserved")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def case_observe_skips_inactive_guild() -> list[str]:
    fails = []
    _reset_buffer()
    path = _write_access(active=False, mod_roles=[], guild_id=42)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        msg = _make_message("m1", 42, 999)
        asyncio.run(bridge._observe_for_mod(msg))
        if bridge._mod_buffer:
            fails.append("b) inactive guild should not buffer")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def case_observe_skips_unconfigured_guild() -> list[str]:
    fails = []
    _reset_buffer()
    path = _write_access(active=True, mod_roles=[], guild_id=42)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        # message from a DIFFERENT guild — not in access.json
        msg = _make_message("m1", 99999, 999)
        asyncio.run(bridge._observe_for_mod(msg))
        if bridge._mod_buffer:
            fails.append("c) unconfigured guild should not buffer")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def case_observe_skips_mod_message() -> list[str]:
    """G1 mod-immunity at observation time — mod messages never enter the
    buffer (saves codex tokens)."""
    fails = []
    _reset_buffer()
    path = _write_access(active=True, mod_roles=["111"], guild_id=42)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        # Author has role 111 → mod
        msg = _make_message("m1", 42, 999, role_ids=["111"])
        asyncio.run(bridge._observe_for_mod(msg))
        if bridge._mod_buffer:
            fails.append("d) mod author should not buffer")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def case_observe_skips_dm() -> list[str]:
    """Messages without a guild (DMs) shouldn't buffer."""
    fails = []
    _reset_buffer()
    msg = types.SimpleNamespace(
        id="m1", author=types.SimpleNamespace(id=999, display_name="u", bot=False, roles=[]),
        guild=None, channel=types.SimpleNamespace(id=1, name="dm"),
        content="hi", reference=None,
    )
    asyncio.run(bridge._observe_for_mod(msg))
    if bridge._mod_buffer:
        fails.append("e) DM should not buffer (no guild)")
    return fails


# ---------------------------------------------------------------------------
# _flush_mod_buffer
# ---------------------------------------------------------------------------

def case_flush_empty_buffer_no_op() -> list[str]:
    fails = []
    _reset_buffer()
    judge_calls = {"n": 0}
    async def _stub_judge(messages, rules_context="", model="gpt-4o-mini", timeout_s=30):
        judge_calls["n"] += 1
        return []
    bridge._codex_judge_batch = _stub_judge
    asyncio.run(bridge._flush_mod_buffer())
    if judge_calls["n"] != 0:
        fails.append("f) empty buffer flush should not invoke codex")
    return fails


def case_flush_drains_and_dispatches() -> list[str]:
    fails = []
    _reset_buffer()
    # Set up access.json so _observe queues the messages.
    path = _write_access(active=True, mod_roles=[], guild_id=42)
    orig_access = bridge.ACCESS_FILE
    bridge.ACCESS_FILE = path
    # Patch codex + dispatcher
    judge_calls = {"n": 0, "msgs": None}
    async def _stub_judge(messages, rules_context="", model="gpt-4o-mini", timeout_s=30):
        judge_calls["n"] += 1
        judge_calls["msgs"] = messages
        return [{"msg_id": m["msg_id"], "rule_match": None, "confidence": 0.99, "rationale": ""}
                for m in messages]
    bridge._codex_judge_batch = _stub_judge
    dispatch_calls = {"n": 0, "verdict_count": 0}
    async def _stub_dispatch(verdicts, msgs_by_id, client_ref, off_topic_tracker=None):
        dispatch_calls["n"] += 1
        dispatch_calls["verdict_count"] = len(verdicts)
        return {"acted": 0, "escalated_only": 0, "skipped": len(verdicts)}
    bridge._dispatch_verdicts = _stub_dispatch
    try:
        # Push 3 messages
        for i in range(3):
            msg = _make_message(f"m{i}", 42, 1000 + i, content=f"hi {i}")
            asyncio.run(bridge._observe_for_mod(msg))
        if len(bridge._mod_buffer) != 3:
            fails.append(f"g) buffer should have 3 entries pre-flush, got {len(bridge._mod_buffer)}")
        # Flush
        asyncio.run(bridge._flush_mod_buffer())
        if judge_calls["n"] != 1:
            fails.append("g) flush should invoke codex once")
        if dispatch_calls["n"] != 1:
            fails.append("g) flush should invoke dispatcher once")
        if dispatch_calls["verdict_count"] != 3:
            fails.append(f"g) dispatcher should get 3 verdicts, got {dispatch_calls['verdict_count']}")
        if bridge._mod_buffer:
            fails.append("g) buffer should be drained after flush")
    finally:
        bridge.ACCESS_FILE = orig_access
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def case_flush_codex_failure_doesnt_drop_state() -> list[str]:
    """If codex throws, the flush should log + return without crashing
    or losing buffer state silently. (Buffer IS drained at lock-release;
    this is acceptable since we'd rather drop a batch than re-judge
    the same messages forever.)"""
    fails = []
    _reset_buffer()
    path = _write_access(active=True, mod_roles=[], guild_id=42)
    orig_access = bridge.ACCESS_FILE
    bridge.ACCESS_FILE = path
    async def _stub_judge_raises(messages, rules_context="", model="gpt-4o-mini", timeout_s=30):
        raise RuntimeError("codex went down")
    bridge._codex_judge_batch = _stub_judge_raises
    try:
        msg = _make_message("m1", 42, 999)
        asyncio.run(bridge._observe_for_mod(msg))
        # Should not raise
        try:
            asyncio.run(bridge._flush_mod_buffer())
        except Exception as e:
            fails.append(f"h) flush should swallow codex errors, raised {e}")
    finally:
        bridge.ACCESS_FILE = orig_access
        path.unlink(missing_ok=True)
        _reset_buffer()
    return fails


def main() -> int:
    cases = [
        ("a-buffer-normal", case_observe_buffers_normal_message),
        ("b-skip-inactive", case_observe_skips_inactive_guild),
        ("c-skip-unconfigured", case_observe_skips_unconfigured_guild),
        ("d-skip-mod", case_observe_skips_mod_message),
        ("e-skip-dm", case_observe_skips_dm),
        ("f-flush-empty", case_flush_empty_buffer_no_op),
        ("g-flush-drains", case_flush_drains_and_dispatches),
        ("h-codex-fails", case_flush_codex_failure_doesnt_drop_state),
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
    print("\nAll mod-judge buffer/flush invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
