#!/usr/bin/env python3
"""
Tests for the AG2 auto-mod LLM-judge helpers in discord-bridge.py.

Per `notes/ag2-moderator-policy.md` §6.1: codex+gpt-4o-mini batched judge
with 7 rules + G1 (mod immunity) + G2 (confidence gate). This PR ships the
helpers; this file locks in their invariants. The on_message hook + action
dispatchers (and the codex subprocess invocation itself) come in a follow-up.

Helpers under test:
- _load_mod_config(guild_id) — per-guild active/roles from access.json
- _is_moderator(member, role_ids) — G1 mod-immunity check
- _should_auto_action(verdict, threshold) — G2 confidence gate
- _parse_judge_output(json_str) — codex JSON output → verdicts

Run: python3 tests/discord-bridge-mod-judge.test.py
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

# Stub minimal discord module so the bridge imports cleanly without the lib.
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
# _load_mod_config
# ---------------------------------------------------------------------------

def case_load_mod_config_active() -> list[str]:
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({
            "guilds": {
                "1153072414184452236": {
                    "mod_active": True,
                    "moderator_roles": ["111", "222"],
                }
            }
        }, f)
        path = Path(f.name)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        active, roles = bridge._load_mod_config(1153072414184452236)
        if active is not True:
            fails.append(f"a) mod_active should be True, got {active}")
        if set(roles) != {"111", "222"}:
            fails.append(f"a) moderator_roles should be {{'111','222'}}, got {set(roles)}")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
    return fails


def case_load_mod_config_default_inactive() -> list[str]:
    """Guild not configured at all → (False, []). Safe default — bridge
    does NOTHING for guilds without explicit opt-in."""
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"guilds": {}}, f)
        path = Path(f.name)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        active, roles = bridge._load_mod_config(99999)
        if active is not False:
            fails.append("b) unconfigured guild should default to mod_active=False")
        if roles != []:
            fails.append(f"b) unconfigured guild should default to roles=[], got {roles}")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
    return fails


def case_load_mod_config_missing_file() -> list[str]:
    fails = []
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = Path("/nonexistent/path/access.json")
        active, roles = bridge._load_mod_config(123)
        if active is not False or roles != []:
            fails.append("c) missing access.json should give (False, [])")
    finally:
        bridge.ACCESS_FILE = orig
    return fails


def case_load_mod_config_malformed_json() -> list[str]:
    fails = []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{ this is not json")
        path = Path(f.name)
    orig = bridge.ACCESS_FILE
    try:
        bridge.ACCESS_FILE = path
        active, roles = bridge._load_mod_config(123)
        if active is not False or roles != []:
            fails.append("d) malformed access.json should give (False, [])")
    finally:
        bridge.ACCESS_FILE = orig
        path.unlink(missing_ok=True)
    return fails


# ---------------------------------------------------------------------------
# _is_moderator (G1 mod-immunity gate)
# ---------------------------------------------------------------------------

def _make_member(member_id, role_ids, guild_owner_id=None):
    roles = [types.SimpleNamespace(id=r) for r in role_ids]
    guild = types.SimpleNamespace(owner_id=guild_owner_id) if guild_owner_id else None
    return types.SimpleNamespace(id=member_id, roles=roles, guild=guild)


def case_is_moderator_via_role() -> list[str]:
    fails = []
    member = _make_member(999, ["111", "222"])
    if not bridge._is_moderator(member, ["111"]):
        fails.append("e) member with role 111 should be mod when 111 is in mod_role_ids")
    if bridge._is_moderator(member, ["333"]):
        fails.append("e) member without any matching role should NOT be mod")
    return fails


def case_is_moderator_owner_immune() -> list[str]:
    """Guild owner is always treated as mod, even if they don't have the
    mod role explicitly."""
    fails = []
    member = _make_member(999, [], guild_owner_id=999)
    if not bridge._is_moderator(member, []):
        fails.append("f) guild owner should always be considered mod")
    return fails


def case_is_moderator_none_safe() -> list[str]:
    fails = []
    if bridge._is_moderator(None, ["111"]):
        fails.append("g) None member should not be mod (defensive)")
    return fails


def case_is_moderator_handles_string_role_ids() -> list[str]:
    """member.roles[i].id may be int (real Discord) or string (test mocks);
    mod_role_ids may be either too. The check should normalize."""
    fails = []
    member_int_roles = _make_member(999, [111, 222])
    if not bridge._is_moderator(member_int_roles, ["111"]):
        fails.append("h) int role IDs should match string mod_role_ids")
    member_str_roles = _make_member(999, ["111", "222"])
    if not bridge._is_moderator(member_str_roles, ["111"]):
        fails.append("h) string role IDs should match string mod_role_ids")
    return fails


# ---------------------------------------------------------------------------
# _should_auto_action (G2 confidence gate)
# ---------------------------------------------------------------------------

def case_should_auto_action_above_threshold() -> list[str]:
    fails = []
    # rule_1 default threshold = 0.85
    v = {"msg_id": "x", "rule_match": "rule_1", "confidence": 0.92, "rationale": "..."}
    if not bridge._should_auto_action(v):
        fails.append("i) high-confidence rule_1 verdict should auto-action")
    return fails


def case_should_auto_action_below_threshold() -> list[str]:
    fails = []
    v = {"msg_id": "x", "rule_match": "rule_1", "confidence": 0.50, "rationale": "..."}
    if bridge._should_auto_action(v):
        fails.append("j) below-threshold verdict should NOT auto-action (G2 → escalate-only)")
    return fails


def case_should_auto_action_rule_specific_threshold() -> list[str]:
    """rule_5 has threshold 0.90; 0.87 should be below."""
    fails = []
    v = {"msg_id": "x", "rule_match": "rule_5", "confidence": 0.87, "rationale": "..."}
    if bridge._should_auto_action(v):
        fails.append("k) rule_5 needs ≥0.90; 0.87 should not auto-action")
    v = {"msg_id": "x", "rule_match": "rule_5", "confidence": 0.95, "rationale": "..."}
    if not bridge._should_auto_action(v):
        fails.append("k) rule_5 at 0.95 should auto-action")
    return fails


def case_should_auto_action_no_match() -> list[str]:
    fails = []
    v = {"msg_id": "x", "rule_match": None, "confidence": 0.99, "rationale": "ok"}
    if bridge._should_auto_action(v):
        fails.append("l) no rule_match should never auto-action regardless of confidence")
    return fails


def case_should_auto_action_malformed() -> list[str]:
    fails = []
    if bridge._should_auto_action(None):
        fails.append("m) None verdict should not auto-action")
    if bridge._should_auto_action({"rule_match": "rule_1", "confidence": "not-a-float"}):
        fails.append("m) malformed confidence should not auto-action")
    return fails


# ---------------------------------------------------------------------------
# _parse_judge_output
# ---------------------------------------------------------------------------

def case_parse_judge_output_list_form() -> list[str]:
    fails = []
    j = json.dumps([
        {"msg_id": "111", "rule_match": "rule_1", "confidence": 0.92, "rationale": "spam"},
        {"msg_id": "222", "rule_match": None, "confidence": 0.98, "rationale": "ok"},
    ])
    out = bridge._parse_judge_output(j)
    if len(out) != 2:
        fails.append(f"n) should parse 2 verdicts, got {len(out)}")
    if out and out[0]["msg_id"] != "111":
        fails.append("n) msg_id ordering preserved")
    return fails


def case_parse_judge_output_wrapped_form() -> list[str]:
    """Codex sometimes wraps output as {"verdicts": [...]} — accept both."""
    fails = []
    j = json.dumps({"verdicts": [
        {"msg_id": "111", "rule_match": "rule_2", "confidence": 0.91, "rationale": ""},
    ]})
    out = bridge._parse_judge_output(j)
    if len(out) != 1:
        fails.append("o) wrapped form should parse")
    return fails


def case_parse_judge_output_malformed() -> list[str]:
    fails = []
    if bridge._parse_judge_output("not json") != []:
        fails.append("p) non-JSON should parse to []")
    if bridge._parse_judge_output("") != []:
        fails.append("p) empty string should parse to []")
    if bridge._parse_judge_output(None) != []:
        fails.append("p) None should parse to []")
    return fails


def case_parse_judge_output_drops_invalid_entries() -> list[str]:
    """Mixed valid/invalid entries: keep the valid ones, drop the bad."""
    fails = []
    j = json.dumps([
        {"msg_id": "111", "rule_match": "rule_1", "confidence": 0.9, "rationale": ""},
        {"rule_match": "rule_1", "confidence": 0.9},  # missing msg_id
        {"msg_id": "333", "confidence": "bad"},  # bad confidence
        {"msg_id": "444", "rule_match": 42, "confidence": 0.8},  # bad rule_match type
        {"msg_id": "555", "rule_match": None, "confidence": 0.99, "rationale": "clean"},
    ])
    out = bridge._parse_judge_output(j)
    msg_ids = [v["msg_id"] for v in out]
    if msg_ids != ["111", "555"]:
        fails.append(f"q) should keep only valid entries 111 + 555, got {msg_ids}")
    return fails


def case_parse_judge_output_clamps_confidence() -> list[str]:
    """Confidence outside [0,1] is clamped, not rejected."""
    fails = []
    j = json.dumps([
        {"msg_id": "111", "rule_match": "rule_1", "confidence": 1.5, "rationale": ""},
        {"msg_id": "222", "rule_match": "rule_1", "confidence": -0.3, "rationale": ""},
    ])
    out = bridge._parse_judge_output(j)
    if len(out) != 2 or out[0]["confidence"] != 1.0 or out[1]["confidence"] != 0.0:
        fails.append(f"r) confidence should clamp to [0,1], got {[v['confidence'] for v in out]}")
    return fails


def main() -> int:
    cases = [
        ("a-active", case_load_mod_config_active),
        ("b-default", case_load_mod_config_default_inactive),
        ("c-no-file", case_load_mod_config_missing_file),
        ("d-malformed", case_load_mod_config_malformed_json),
        ("e-via-role", case_is_moderator_via_role),
        ("f-owner", case_is_moderator_owner_immune),
        ("g-none", case_is_moderator_none_safe),
        ("h-id-coerce", case_is_moderator_handles_string_role_ids),
        ("i-above", case_should_auto_action_above_threshold),
        ("j-below", case_should_auto_action_below_threshold),
        ("k-rule-spec", case_should_auto_action_rule_specific_threshold),
        ("l-no-match", case_should_auto_action_no_match),
        ("m-malformed", case_should_auto_action_malformed),
        ("n-list", case_parse_judge_output_list_form),
        ("o-wrapped", case_parse_judge_output_wrapped_form),
        ("p-malformed", case_parse_judge_output_malformed),
        ("q-drops", case_parse_judge_output_drops_invalid_entries),
        ("r-clamp", case_parse_judge_output_clamps_confidence),
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
    print("\nAll mod-judge helper invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
