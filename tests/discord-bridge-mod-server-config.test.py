#!/usr/bin/env python3
"""
Tests for `_load_mod_server_config(guild_id)` in discord-bridge.py — the
per-guild config reader that replaces the old AG2-hardcoded MOD_ESCALATION_*
constants.

Run: python3 tests/discord-bridge-mod-server-config.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
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
    def __init__(self, *a, **kw):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
    def event(self, fn): return fn


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None


class _DMChannel: pass


_discord_stub.DMChannel = _DMChannel
sys.modules["discord"] = _discord_stub


def load_bridge_with_access_json(content: dict | None):
    """Load discord-bridge with ACCESS_FILE pointed at a temp file."""
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
    if content is None:
        bridge.ACCESS_FILE = Path("/tmp/_definitely-does-not-exist-99999")
    else:
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(content, tf)
        tf.close()
        bridge.ACCESS_FILE = Path(tf.name)
    return bridge


def case_a_no_access_file():
    """When access.json doesn't exist, all fields default safely."""
    fails = []
    bridge = load_bridge_with_access_json(None)
    cfg = bridge._load_mod_server_config(123)
    if cfg["escalation_channel"] is not None:
        fails.append("a) missing access.json should default escalation_channel to None")
    if cfg["escalation_ccs"] != ():
        fails.append("a) missing access.json should default escalation_ccs to ()")
    if cfg["redirect_channel_jobs"] is not None:
        fails.append("a) missing access.json should default redirect_channel_jobs to None")
    return fails


def case_b_no_guilds_key():
    fails = []
    bridge = load_bridge_with_access_json({"groups": {}})
    cfg = bridge._load_mod_server_config(123)
    if cfg["escalation_channel"] is not None:
        fails.append("b) missing guilds key should default escalation_channel to None")
    if cfg["escalation_ccs"] != ():
        fails.append("b) missing guilds key should default escalation_ccs to ()")
    return fails


def case_c_guild_not_listed():
    fails = []
    bridge = load_bridge_with_access_json({"guilds": {"999": {"escalation_channel": 555}}})
    cfg = bridge._load_mod_server_config(123)
    if cfg["escalation_channel"] is not None:
        fails.append("c) unlisted guild should not inherit other guild's config")
    return fails


def case_d_full_config_lookup():
    """Happy path — escalation_channel, escalation_cc_user_ids, redirect_channel_jobs all present."""
    fails = []
    bridge = load_bridge_with_access_json({
        "guilds": {
            "1153072414184452236": {
                "mod_active": True,
                "moderator_roles": ["1234"],
                "escalation_channel": 1153753796321738863,
                "escalation_cc_user_ids": ["1022910063620390932", "1025828152183885925"],
                "redirect_channel_jobs": 1168719200605437952,
            }
        }
    })
    cfg = bridge._load_mod_server_config(1153072414184452236)
    if cfg["escalation_channel"] != 1153753796321738863:
        fails.append(f"d) escalation_channel mismatch: got {cfg['escalation_channel']}")
    if cfg["escalation_ccs"] != ("<@1022910063620390932>", "<@1025828152183885925>"):
        fails.append(f"d) escalation_ccs mismatch: got {cfg['escalation_ccs']}")
    if cfg["redirect_channel_jobs"] != 1168719200605437952:
        fails.append(f"d) redirect_channel_jobs mismatch: got {cfg['redirect_channel_jobs']}")
    return fails


def case_e_string_guild_id():
    """guild_id passed as int should still match string-keyed access.json."""
    fails = []
    bridge = load_bridge_with_access_json({"guilds": {"42": {"escalation_channel": 99}}})
    cfg = bridge._load_mod_server_config(42)  # int
    if cfg["escalation_channel"] != 99:
        fails.append(f"e) int guild_id should match string key: got {cfg['escalation_channel']}")
    return fails


def case_f_malformed_cc_list():
    """When escalation_cc_user_ids is malformed (not a list), default to ()."""
    fails = []
    bridge = load_bridge_with_access_json({"guilds": {"42": {"escalation_cc_user_ids": "not-a-list"}}})
    cfg = bridge._load_mod_server_config(42)
    if cfg["escalation_ccs"] != ():
        fails.append(f"f) malformed cc list should default to (): got {cfg['escalation_ccs']}")
    return fails


def case_g_int_user_ids_in_cc():
    """If user_ids are stored as int (not str), still work."""
    fails = []
    bridge = load_bridge_with_access_json({
        "guilds": {"42": {"escalation_cc_user_ids": [123, 456]}}
    })
    cfg = bridge._load_mod_server_config(42)
    if cfg["escalation_ccs"] != ("<@123>", "<@456>"):
        fails.append(f"g) int user_ids should still produce mention strings: got {cfg['escalation_ccs']}")
    return fails


def case_h_partial_config():
    """When only some fields are configured, others default to None/()."""
    fails = []
    bridge = load_bridge_with_access_json({
        "guilds": {"42": {"escalation_channel": 555}}  # only escalation_channel
    })
    cfg = bridge._load_mod_server_config(42)
    if cfg["escalation_channel"] != 555:
        fails.append(f"h) partial config: escalation_channel should be 555, got {cfg['escalation_channel']}")
    if cfg["escalation_ccs"] != ():
        fails.append(f"h) partial config: missing ccs should default to ()")
    if cfg["redirect_channel_jobs"] is not None:
        fails.append(f"h) partial config: missing redirect should default to None")
    return fails


def case_i_guild_id_of_message():
    """_guild_id_of: extracts message.guild.id when present, else None."""
    fails = []
    bridge = load_bridge_with_access_json({})
    msg_with_guild = types.SimpleNamespace(guild=types.SimpleNamespace(id=42))
    if bridge._guild_id_of(msg_with_guild) != 42:
        fails.append("i) should extract guild.id from message")
    msg_no_guild = types.SimpleNamespace(guild=None)
    if bridge._guild_id_of(msg_no_guild) is not None:
        fails.append("i) message.guild=None should return None")
    msg_no_attr = types.SimpleNamespace()
    if bridge._guild_id_of(msg_no_attr) is not None:
        fails.append("i) message without guild attr should return None")
    return fails


def main():
    cases = [
        ("a-no-access-file", case_a_no_access_file),
        ("b-no-guilds-key", case_b_no_guilds_key),
        ("c-guild-not-listed", case_c_guild_not_listed),
        ("d-full-config-lookup", case_d_full_config_lookup),
        ("e-string-guild-id", case_e_string_guild_id),
        ("f-malformed-cc-list", case_f_malformed_cc_list),
        ("g-int-user-ids-in-cc", case_g_int_user_ids_in_cc),
        ("h-partial-config", case_h_partial_config),
        ("i-guild-id-of-message", case_i_guild_id_of_message),
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
    print("\nAll mod-judge server-config invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
