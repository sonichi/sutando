#!/usr/bin/env python3
"""
Tests for the first-post welcome helper logic in discord-bridge.py.

Per msze 2026-05-06: drop the on_member_join approach (#623) — fire welcome
when a new user posts their first message in the guild's configured
welcome_channel. No privileged Server Members Intent needed.

Helpers under test (pure / testable):
  - _load_welcome_channel(guild_id): per-guild lookup
  - _read_welcome_template(): canonical-template file read
  - _load_welcomed_users() / _mark_user_welcomed(): persisted dedup state
  - _is_user_welcomed(): pure dedup check
  - _should_welcome_first_post(): full gating decision

The async on_message hook is integration-tested via these helpers — caller
chains them in the natural order and short-circuits when the gate refuses.

Run: python3 tests/discord-bridge-welcome-on-first-post.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub minimal discord module BEFORE the bridge is exec'd.
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


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None


class _DMChannel:
    pass


_discord_stub.DMChannel = _DMChannel
sys.modules["discord"] = _discord_stub


def load_bridge():
    """Exec the bridge module without running its main()."""
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


def _make_message(guild_id, channel_id, author_id, is_bot=False):
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=guild_id),
        channel=types.SimpleNamespace(id=channel_id),
        author=types.SimpleNamespace(id=author_id, bot=is_bot),
    )


# ---------------------------------------------------------------------------
# _load_welcome_channel
# ---------------------------------------------------------------------------

def case_load_welcome_channel_per_guild() -> list[str]:
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"guilds": {"1153072414184452236": {"welcome_channel": "1307539994751139893", "welcome_template": "/tmp/t.md"}}}, f)
        path = Path(f.name)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        if bridge._load_welcome_channel(1153072414184452236) != 1307539994751139893:
            fails.append("a) configured guild should resolve to int channel id")
        if bridge._load_welcome_channel(99999999999999999) is not None:
            fails.append("a) unconfigured guild should return None")
        # Per-guild template path
        ch, tpl = bridge._load_welcome_config(1153072414184452236)
        if tpl != "/tmp/t.md":
            fails.append(f"a) per-guild welcome_template should resolve, got {tpl!r}")
        # Channel-only (no template) → tpl=None
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f2:
            json.dump({"guilds": {"42": {"welcome_channel": "100"}}}, f2)
            ch_only_path = Path(f2.name)
        bridge.ACCESS_FILE = ch_only_path
        ch, tpl = bridge._load_welcome_config(42)
        if ch != 100 or tpl is not None:
            fails.append(f"a) channel-only config should give (100, None); got ({ch}, {tpl})")
        ch_only_path.unlink(missing_ok=True)
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
    return fails


def case_load_welcome_channel_missing_or_malformed() -> list[str]:
    fails = []
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = Path("/nonexistent/path/access.json")
        if bridge._load_welcome_channel(123) is not None:
            fails.append("b) missing access.json should return None")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{ this is not json")
            mal_path = Path(f.name)
        bridge.ACCESS_FILE = mal_path
        if bridge._load_welcome_channel(123) is not None:
            fails.append("b) malformed access.json should return None")
        mal_path.unlink(missing_ok=True)
    finally:
        bridge.ACCESS_FILE = orig
    return fails


# ---------------------------------------------------------------------------
# _read_welcome_template
# ---------------------------------------------------------------------------

def case_read_welcome_template_round_trip() -> list[str]:
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write("Hello body\n")
        path = Path(f.name)
    try:
        # Caller passes the path explicitly; no module-level constant.
        if bridge._read_welcome_template(str(path)) != "Hello body\n":
            fails.append("c) template content not returned")
    finally:
        path.unlink(missing_ok=True)
    return fails


def case_read_welcome_template_missing_returns_empty() -> list[str]:
    fails = []
    if bridge._read_welcome_template("/nonexistent/welcome.md") != "":
        fails.append("d) missing template should return empty (skip-welcome signal)")
    if bridge._read_welcome_template(None) != "":
        fails.append("d) None path should return empty (no bridge-side default)")
    if bridge._read_welcome_template("") != "":
        fails.append("d) empty string path should return empty")
    return fails


# ---------------------------------------------------------------------------
# _load_welcomed_users / _mark_user_welcomed (round-trip persistence)
# ---------------------------------------------------------------------------

def case_welcomed_users_round_trip() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        orig_state = bridge.STATE_DIR
        orig_file = bridge.WELCOMED_USERS_FILE
        try:
            bridge.STATE_DIR = td
            bridge.WELCOMED_USERS_FILE = td / "discord-welcomed-users.json"
            # Empty initial state
            if bridge._load_welcomed_users() != {}:
                fails.append("e) fresh state should be empty dict")
            # Mark user A in guild G1
            bridge._mark_user_welcomed(1, 100)
            current = bridge._load_welcomed_users()
            if current.get("1") != {"100"}:
                fails.append(f"e) after mark, expected {{'1': {{'100'}}}}, got {current}")
            # Mark user B in guild G1 — should preserve user A
            bridge._mark_user_welcomed(1, 200)
            current = bridge._load_welcomed_users()
            if current.get("1") != {"100", "200"}:
                fails.append(f"e) after second mark same guild, expected both users, got {current.get('1')}")
            # Mark user C in different guild — should add new key
            bridge._mark_user_welcomed(2, 300)
            current = bridge._load_welcomed_users()
            if current.get("2") != {"300"}:
                fails.append(f"e) different guild not added correctly: {current}")
            if current.get("1") != {"100", "200"}:
                fails.append("e) original guild's users lost on different-guild mark")
        finally:
            bridge.STATE_DIR = orig_state
            bridge.WELCOMED_USERS_FILE = orig_file
    return fails


def case_welcomed_users_malformed_json() -> list[str]:
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{ this is not json")
        path = Path(f.name)
    orig = bridge.WELCOMED_USERS_FILE
    try:
        bridge.WELCOMED_USERS_FILE = path
        if bridge._load_welcomed_users() != {}:
            fails.append("f) malformed welcomed-users.json should return empty dict")
    finally:
        bridge.WELCOMED_USERS_FILE = orig
        path.unlink(missing_ok=True)
    return fails


# ---------------------------------------------------------------------------
# _is_user_welcomed
# ---------------------------------------------------------------------------

def case_is_user_welcomed_check() -> list[str]:
    fails = []
    welcomed = {"1": {"100", "200"}, "2": {"300"}}
    if not bridge._is_user_welcomed(1, 100, welcomed):
        fails.append("g) known user in guild 1 should be welcomed")
    if bridge._is_user_welcomed(1, 999, welcomed):
        fails.append("g) unknown user in known guild should not be welcomed")
    if bridge._is_user_welcomed(99, 100, welcomed):
        fails.append("g) known user_id but unknown guild should not be welcomed")
    if bridge._is_user_welcomed(1, 100, {}):
        fails.append("g) empty welcomed dict should never report welcomed")
    return fails


# ---------------------------------------------------------------------------
# _should_welcome_first_post (the gating decision)
# ---------------------------------------------------------------------------

def case_should_welcome_happy_path() -> list[str]:
    fails = []
    msg = _make_message(guild_id=1, channel_id=100, author_id=999)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users={})
    if not do:
        fails.append(f"h) happy path should welcome, reason={reason}")
    return fails


def case_should_welcome_skip_reasons() -> list[str]:
    fails = []
    # No guild → DM/private context → skip
    msg = types.SimpleNamespace(
        guild=None,
        channel=types.SimpleNamespace(id=100),
        author=types.SimpleNamespace(id=999, bot=False),
    )
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users={})
    if do or reason != "no_guild":
        fails.append(f"i) no_guild expected, got do={do} reason={reason}")
    # No welcome channel configured → skip
    msg = _make_message(1, 100, 999)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=None, welcome_template_path="/tmp/t.md", welcomed_users={})
    if do or reason != "no_welcome_channel_configured":
        fails.append(f"i) no_welcome_channel expected, got do={do} reason={reason}")
    # No welcome template configured → skip
    msg = _make_message(1, 100, 999)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path=None, welcomed_users={})
    if do or reason != "no_welcome_template_configured":
        fails.append(f"i) no_welcome_template expected, got do={do} reason={reason}")
    # Wrong channel (message in #general, welcome in #welcome) → skip
    msg = _make_message(1, 999, 1234)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users={})
    if do or reason != "wrong_channel":
        fails.append(f"i) wrong_channel expected, got do={do} reason={reason}")
    # Bot author → skip
    msg = _make_message(1, 100, 999, is_bot=True)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users={})
    if do or reason != "bot_account":
        fails.append(f"i) bot_account expected, got do={do} reason={reason}")
    # Already welcomed → skip
    msg = _make_message(1, 100, 999)
    do, reason = bridge._should_welcome_first_post(msg, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users={"1": {"999"}})
    if do or reason != "already_welcomed":
        fails.append(f"i) already_welcomed expected, got do={do} reason={reason}")
    return fails


def case_dedup_across_two_messages() -> list[str]:
    """First post welcomes; second post from same user in same guild does not."""
    fails = []
    welcomed = {}
    msg1 = _make_message(1, 100, 999)
    do1, _ = bridge._should_welcome_first_post(msg1, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users=welcomed)
    if not do1:
        fails.append("j) first post should welcome")
    # Caller would then mark welcomed before processing next msg
    welcomed.setdefault("1", set()).add("999")
    msg2 = _make_message(1, 100, 999)
    do2, reason2 = bridge._should_welcome_first_post(msg2, welcome_channel_id=100, welcome_template_path="/tmp/t.md", welcomed_users=welcomed)
    if do2:
        fails.append(f"j) second post should NOT welcome, reason={reason2}")
    return fails


def main() -> int:
    cases = [
        ("a-perguild", case_load_welcome_channel_per_guild),
        ("b-missing", case_load_welcome_channel_missing_or_malformed),
        ("c-tmpl", case_read_welcome_template_round_trip),
        ("d-tmpl-missing", case_read_welcome_template_missing_returns_empty),
        ("e-roundtrip", case_welcomed_users_round_trip),
        ("f-malformed-state", case_welcomed_users_malformed_json),
        ("g-iswelcomed", case_is_user_welcomed_check),
        ("h-happy", case_should_welcome_happy_path),
        ("i-skips", case_should_welcome_skip_reasons),
        ("j-dedup", case_dedup_across_two_messages),
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
    print("\nAll first-post welcome invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
