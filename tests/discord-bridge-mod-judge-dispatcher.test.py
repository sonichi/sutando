#!/usr/bin/env python3
"""
Tests for the verdict dispatcher in discord-bridge.py.

PR5 of 6 — covers `_dispatch_verdicts`. PR6 wires the on_message hook +
buffer + flush timer.

Run: python3 tests/discord-bridge-mod-judge-dispatcher.test.py
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


# ---------------------------------------------------------------------------
# Mocks — the dispatcher calls action helpers; capture action types.
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, name="g", channel_id=42):
        self.name = name
        self.id = channel_id
        self.sent = []
    async def send(self, content):
        self.sent.append(content)
        return types.SimpleNamespace(id=12345)


class _MockMessage:
    def __init__(self, msg_id, content="x", channel=None, author_id=999):
        self.id = msg_id
        self.author = types.SimpleNamespace(id=author_id, display_name="u")
        self.channel = channel or _MockChannel()
        self.content = content
        self.jump_url = "https://discord/jump"
        self.deleted = False
    async def delete(self):
        self.deleted = True


def _patch_actions(bridge_mod):
    """Replace each action with a counter so we can assert which ran."""
    counters = {"delete_and_escalate": 0, "redirect_to_jobs": 0,
                "escalate_only": 0, "polite_reminder": 0}

    async def _del(client, msg, verdict, extras_md=""):
        counters["delete_and_escalate"] += 1
        return True, types.SimpleNamespace()
    async def _red(client, msg, verdict):
        counters["redirect_to_jobs"] += 1
        return True, types.SimpleNamespace()
    async def _esc(client, msg, verdict, extras_md=""):
        counters["escalate_only"] += 1
        return types.SimpleNamespace()
    async def _rem(channel, channel_topic_hint=None):
        counters["polite_reminder"] += 1

    bridge_mod._action_delete_and_escalate = _del
    bridge_mod._action_redirect_to_jobs = _red
    bridge_mod._action_escalate_only = _esc
    bridge_mod._action_polite_reminder = _rem
    return counters


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_dispatch_rule_1_delete() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("111")
    msgs = {"111": msg}
    verdicts = [{"msg_id": "111", "rule_match": "rule_1", "confidence": 0.92, "rationale": ""}]
    summary = asyncio.run(bridge._dispatch_verdicts(verdicts, msgs, client_ref=None))
    if counters["delete_and_escalate"] != 1:
        fails.append(f"a) rule_1 should trigger delete_and_escalate, got {counters}")
    if summary["acted"] != 1:
        fails.append(f"a) summary acted=1 expected, got {summary}")
    return fails


def case_dispatch_rule_4_redirect() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("222")
    verdicts = [{"msg_id": "222", "rule_match": "rule_4", "confidence": 0.90, "rationale": ""}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"222": msg}, client_ref=None))
    if counters["redirect_to_jobs"] != 1:
        fails.append("b) rule_4 should trigger redirect_to_jobs")
    return fails


def case_dispatch_rule_5_escalate_only() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("333")
    verdicts = [{"msg_id": "333", "rule_match": "rule_5", "confidence": 0.95, "rationale": ""}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"333": msg}, client_ref=None))
    if counters["escalate_only"] != 1:
        fails.append("c) rule_5 should trigger escalate_only")
    if counters["delete_and_escalate"] != 0:
        fails.append("c) rule_5 should NOT trigger delete")
    return fails


def case_dispatch_below_threshold_falls_to_escalate() -> list[str]:
    """G2: low-confidence verdict for a delete-rule still gets escalate-only,
    not delete."""
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("444")
    verdicts = [{"msg_id": "444", "rule_match": "rule_1", "confidence": 0.40, "rationale": ""}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"444": msg}, client_ref=None))
    if counters["escalate_only"] != 1:
        fails.append("d) below-threshold rule_1 should fall to escalate_only")
    if counters["delete_and_escalate"] != 0:
        fails.append("d) below-threshold should NOT delete")
    return fails


def case_dispatch_rule_3_violates() -> list[str]:
    """Rule 3 with violates_server_rules=True → delete + escalate."""
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("555")
    verdicts = [{"msg_id": "555", "rule_match": "rule_3", "confidence": 0.90,
                 "rationale": "", "violates_server_rules": True}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"555": msg}, client_ref=None))
    if counters["delete_and_escalate"] != 1:
        fails.append("e) rule_3 + violates_server_rules → delete")
    return fails


def case_dispatch_rule_3_legit_crosspost() -> list[str]:
    """Rule 3 with violates_server_rules=False → escalate-only."""
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("666")
    verdicts = [{"msg_id": "666", "rule_match": "rule_3", "confidence": 0.92,
                 "rationale": "", "violates_server_rules": False}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"666": msg}, client_ref=None))
    if counters["escalate_only"] != 1:
        fails.append("f) rule_3 + NOT violates → escalate-only, not delete")
    if counters["delete_and_escalate"] != 0:
        fails.append("f) rule_3 + NOT violates should not delete")
    return fails


def case_dispatch_rule_7_no_action_here() -> list[str]:
    """Rule 7 dispatch is a no-op in _dispatch_verdicts — the streak tracker
    + action fire happens upstream at on_message time."""
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("777")
    verdicts = [{"msg_id": "777", "rule_match": "rule_7", "confidence": 0.92, "rationale": ""}]
    summary = asyncio.run(bridge._dispatch_verdicts(verdicts, {"777": msg}, client_ref=None))
    if any(counters[k] for k in counters):
        fails.append(f"g) rule_7 should not trigger any action in dispatcher, got {counters}")
    if summary["skipped"] != 1:
        fails.append(f"g) rule_7 should be skipped, got {summary}")
    return fails


def case_dispatch_clean_message() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("888")
    verdicts = [{"msg_id": "888", "rule_match": None, "confidence": 0.99, "rationale": "ok"}]
    summary = asyncio.run(bridge._dispatch_verdicts(verdicts, {"888": msg}, client_ref=None))
    if any(counters[k] for k in counters):
        fails.append("h) clean verdict should not trigger any action")
    if summary["skipped"] != 1:
        fails.append("h) clean verdict should count as skipped")
    return fails


def case_dispatch_unknown_msg_id_skipped() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    verdicts = [{"msg_id": "999", "rule_match": "rule_1", "confidence": 0.95, "rationale": ""}]
    summary = asyncio.run(bridge._dispatch_verdicts(verdicts, {}, client_ref=None))
    if any(counters[k] for k in counters):
        fails.append("i) verdict with unknown msg_id should not trigger action")
    if summary["skipped"] != 1:
        fails.append("i) unknown msg_id should be counted as skipped")
    return fails


def case_dispatch_unknown_rule_falls_to_escalate() -> list[str]:
    fails = []
    counters = _patch_actions(bridge)
    msg = _MockMessage("aaa")
    verdicts = [{"msg_id": "aaa", "rule_match": "rule_99", "confidence": 0.95, "rationale": ""}]
    asyncio.run(bridge._dispatch_verdicts(verdicts, {"aaa": msg}, client_ref=None))
    if counters["escalate_only"] != 1:
        fails.append("j) unknown rule_match should fall to escalate_only as safe default")
    return fails


def case_dispatch_batch_summary() -> list[str]:
    """Mixed batch: 1 delete, 1 escalate-only, 1 clean, 1 unknown-id."""
    fails = []
    counters = _patch_actions(bridge)
    msg1 = _MockMessage("1")
    msg2 = _MockMessage("2")
    msg3 = _MockMessage("3")
    msgs = {"1": msg1, "2": msg2, "3": msg3}
    verdicts = [
        {"msg_id": "1", "rule_match": "rule_1", "confidence": 0.95, "rationale": ""},
        {"msg_id": "2", "rule_match": "rule_5", "confidence": 0.95, "rationale": ""},
        {"msg_id": "3", "rule_match": None, "confidence": 0.99, "rationale": ""},
        {"msg_id": "404", "rule_match": "rule_1", "confidence": 0.95, "rationale": ""},
    ]
    summary = asyncio.run(bridge._dispatch_verdicts(verdicts, msgs, client_ref=None))
    if summary != {"acted": 1, "escalated_only": 1, "skipped": 2}:
        fails.append(f"k) summary should reflect mixed outcomes, got {summary}")
    return fails


def main() -> int:
    cases = [
        ("a-rule-1", case_dispatch_rule_1_delete),
        ("b-rule-4", case_dispatch_rule_4_redirect),
        ("c-rule-5", case_dispatch_rule_5_escalate_only),
        ("d-g2-fall", case_dispatch_below_threshold_falls_to_escalate),
        ("e-r3-violates", case_dispatch_rule_3_violates),
        ("f-r3-legit", case_dispatch_rule_3_legit_crosspost),
        ("g-r7-noop", case_dispatch_rule_7_no_action_here),
        ("h-clean", case_dispatch_clean_message),
        ("i-unknown-msg", case_dispatch_unknown_msg_id_skipped),
        ("j-unknown-rule", case_dispatch_unknown_rule_falls_to_escalate),
        ("k-batch-summary", case_dispatch_batch_summary),
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
    print("\nAll mod-judge dispatcher invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
