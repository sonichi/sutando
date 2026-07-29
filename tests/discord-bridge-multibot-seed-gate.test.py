#!/usr/bin/env python3
"""Tests for the multi-bot-safe thread auto-seed gate (2026-07-02).

Background — the seed storm / grab pile-up:
In a fleet deployment several Sutando bots watch one guild. The thread
auto-seed (added ungated in #1498 to fix the ep013 first-message drop) fired
on ANY first thread message, so a single owner @-ping made EVERY bot seed the
thread into its own access.json, post its own "🌱 Auto-seeded" notice (each
pinging its own owner), and thereafter treat every follow-up as a task. Result:
N bots piled onto one PR (the 2026-07-02 #1823 collision) and the owner saw a
stack of duplicate 🌱 notices.

The fix keeps the block UNGATED for unknown/single-bot deployments (so the
#1498 ep013 first-message-drop fix stands) and gates it on "this bot is
addressed" after fleet intent is persisted in workspace-owned config:

    _seed_ok = bot_mentioned or role_mentioned or not \
        _thread_seed_requires_addressing(...)
    if thread_id_str not in access_groups and _seed_ok:
        ...auto-seed...

This test has two layers:
  1. BEHAVIORAL — extracts the two pure gate helpers from
     src/discord-bridge.py and exec's them in isolation (no `import discord`,
     matching the other bridge tests' no-heavy-import convention).
  2. STRUCTURAL — asserts the seed block wires the gate in the expected shape
     and does NOT regress to the `bot_mentioned and isinstance(...Thread)`
     form guarded by discord-bridge-thread-auto-seed-ungate.test.py.

Run: python3 tests/discord-bridge-multibot-seed-gate.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import os
import re
import sys

REPO = Path(os.environ.get(
    "SUTANDO_TEST_REPO",
    Path(__file__).resolve().parent.parent,
))
BRIDGE = REPO / "src" / "discord-bridge.py"


def _src() -> str:
    return BRIDGE.read_text()


def _load_gate_helpers():
    """Extract and exec ONLY the gate helpers so the test runs
    without importing discord-bridge.py (which pulls the heavy discord.py dep,
    per the other bridge tests)."""
    src = _src()
    matches = re.findall(
        r"\ndef (_has_sibling_bots|_thread_seed_requires_addressing)"
        r"\(.*?\n(?=\ndef )",
        src,
        re.S,
    )
    assert len(matches) == 2, "could not locate both gate helpers"
    block = re.search(
        r"\ndef _has_sibling_bots\(.*?"
        r"(?=\ndef _format_seed_notice)",
        src,
        re.S,
    )
    assert block, "could not extract gate helper source"

    class _DiscordConfig:
        @staticmethod
        def load_config():
            return {}

    ns: dict = {"discord_config": _DiscordConfig}
    exec(block.group(0), ns)
    return ns["_has_sibling_bots"], ns["_thread_seed_requires_addressing"]


# ── Behavioral: the truth table ──────────────────────────────────────────────

def test_no_key_is_single_bot():
    f, _ = _load_gate_helpers()
    assert f({}, "111") is False, "missing siblingBots must read as single-bot"


def test_empty_list_is_single_bot():
    f, _ = _load_gate_helpers()
    assert f({"siblingBots": []}, "111") is False


def test_only_self_is_single_bot():
    """A fleet-wide list dropped into every bot's config lists self too; after
    removing self, a lone entry means no OTHER bot → single-bot."""
    f, _ = _load_gate_helpers()
    assert f({"siblingBots": ["111"]}, "111") is False
    assert f({"siblingBots": ["111"]}, 111) is False, "self id may be int"


def test_siblings_present_is_multibot():
    f, _ = _load_gate_helpers()
    assert f({"siblingBots": ["111", "222", "333"]}, "111") is True
    assert f({"siblingBots": [222]}, "111") is True, "int/str ids must compare equal"


def test_malformed_is_single_bot():
    """Any malformed value fails safe to single-bot (never suppress seeding on
    a config typo — a mis-typed bare string must NOT be iterated into chars)."""
    f, _ = _load_gate_helpers()
    assert f({"siblingBots": None}, "111") is False
    assert f({"siblingBots": "222"}, "111") is False, \
        "a bare string must be rejected, not iterated into characters"
    assert f({"siblingBots": 222}, "111") is False
    assert f({}, None) is False


def test_durable_addressed_mode_survives_missing_access_field():
    _, gate = _load_gate_helpers()
    assert gate({}, "111", {"threadAutoSeedMode": "addressed"}) is True
    assert gate(
        {"siblingBots": None},
        "111",
        {"threadAutoSeedMode": "addressed", "siblingBots": ["111", "222"]},
    ) is True


def test_explicit_any_mode_preserves_single_bot_behavior():
    _, gate = _load_gate_helpers()
    assert gate(
        {"siblingBots": ["111", "222"]},
        "111",
        {"threadAutoSeedMode": "any"},
    ) is False


def test_unknown_mode_falls_back_to_legacy_access():
    _, gate = _load_gate_helpers()
    assert gate({}, "111", {}) is False
    assert gate({"siblingBots": ["111", "222"]}, "111", {}) is True
    assert gate({}, "111", {"siblingBots": ["111", "222"]}) is True


# ── Structural: the gate is wired and not regressed ──────────────────────────

def test_seed_gate_present():
    src = _src()
    assert "_thread_seed_requires_addressing(" in src, \
        "seed block no longer calls durable multi-bot gate"
    gate = re.compile(
        r"_seed_ok\s*=\s*\(?\s*bot_mentioned\s+or\s+role_mentioned"
        r"\s+or\s+not\s+_thread_seed_requires_addressing",
        re.S)
    assert gate.search(src), \
        "expected seed gate to call _thread_seed_requires_addressing(...)"
    assert re.search(r"thread_id_str not in access_groups and _seed_ok", src), \
        "seed condition must be gated on `... and _seed_ok`"


def test_no_bot_mentioned_isinstance_regression():
    """Must not reintroduce the pre-#1498 `bot_mentioned and isinstance(...
    Thread)` form (that re-opens the ep013 single-bot silent-drop)."""
    src = _src()
    regression = re.compile(
        r"if\s+bot_mentioned\s+and\s+isinstance\(\s*message\.channel\s*,\s*discord\.Thread\s*\)")
    assert not regression.search(src), \
        "REGRESSION: re-added `bot_mentioned and isinstance(...Thread)` gate"
    assert re.search(
        r"if\s+isinstance\(\s*message\.channel\s*,\s*discord\.Thread\s*\)\s*:", src), \
        "outer `if isinstance(message.channel, discord.Thread):` gate missing"


def main():
    tests = [
        test_no_key_is_single_bot,
        test_empty_list_is_single_bot,
        test_only_self_is_single_bot,
        test_siblings_present_is_multibot,
        test_malformed_is_single_bot,
        test_durable_addressed_mode_survives_missing_access_field,
        test_explicit_any_mode_preserves_single_bot_behavior,
        test_unknown_mode_falls_back_to_legacy_access,
        test_seed_gate_present,
        test_no_bot_mentioned_isinstance_regression,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__} — {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
