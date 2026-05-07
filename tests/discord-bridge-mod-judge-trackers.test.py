#!/usr/bin/env python3
"""
Tests for the stateful detectors in discord-bridge.py.

PR4 of 5 — covers `_DupeTracker` (Rule 3 cross-channel-duplicate) and
`_OffTopicStreakTracker` (Rule 7). PR5 wires the buffer + on_message hook
+ verdict dispatcher.

Run: python3 tests/discord-bridge-mod-judge-trackers.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
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
# _normalize_msg_text
# ---------------------------------------------------------------------------

def case_normalize_basic() -> list[str]:
    fails = []
    if bridge._normalize_msg_text("  Hello   World\t!  ") != "hello world !":
        fails.append("a) lowercase + whitespace-collapse + trim expected")
    if bridge._normalize_msg_text("") != "":
        fails.append("a) empty input → empty")
    if bridge._normalize_msg_text(None) != "":
        fails.append("a) None input → empty (defensive)")
    if len(bridge._normalize_msg_text("x" * 1000)) != 200:
        fails.append("a) 200-char cap")
    return fails


# ---------------------------------------------------------------------------
# _DupeTracker (Rule 3)
# ---------------------------------------------------------------------------

def case_dupe_below_threshold_no_trigger() -> list[str]:
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    # Same user posts same text in 2 channels — under threshold (3)
    r = t.add("u1", "ch_a", "m1", "join us!", now_s=100)
    if r is not None:
        fails.append("b) 1 channel should not trigger")
    r = t.add("u1", "ch_b", "m2", "join us!", now_s=110)
    if r is not None:
        fails.append("b) 2 channels should not trigger (threshold=3)")
    return fails


def case_dupe_at_threshold_triggers() -> list[str]:
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "join discord.gg/spam", now_s=100)
    t.add("u1", "ch_b", "m2", "join discord.gg/spam", now_s=110)
    r = t.add("u1", "ch_c", "m3", "join discord.gg/spam", now_s=120)
    if r is None:
        fails.append("c) 3 distinct channels should trigger")
        return fails
    triggered_msgs = {m for (_c, m) in r}
    if triggered_msgs != {"m1", "m2", "m3"}:
        fails.append(f"c) trigger should return all 3 msg_ids, got {triggered_msgs}")
    return fails


def case_dupe_window_expiry() -> list[str]:
    """Old entries outside window don't count toward channel threshold."""
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "spam text", now_s=0)
    t.add("u1", "ch_b", "m2", "spam text", now_s=10)
    # Third post 400s later — first two are outside window
    r = t.add("u1", "ch_c", "m3", "spam text", now_s=400)
    if r is not None:
        fails.append("d) old entries outside window should not count toward threshold")
    return fails


def case_dupe_different_text_separate_keys() -> list[str]:
    """Different normalized texts → separate keys → don't combine into a trigger."""
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "buy crypto here", now_s=100)
    t.add("u1", "ch_b", "m2", "free vbucks here", now_s=110)
    r = t.add("u1", "ch_c", "m3", "click for prize", now_s=120)
    if r is not None:
        fails.append("e) 3 different texts should NOT trigger (different keys)")
    return fails


def case_dupe_same_channel_doesnt_double_count() -> list[str]:
    """User posting the same text 3 times in the SAME channel doesn't trigger
    Rule 3 — Rule 3 is about cross-channel."""
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "spam", now_s=100)
    t.add("u1", "ch_a", "m2", "spam", now_s=110)
    r = t.add("u1", "ch_a", "m3", "spam", now_s=120)
    if r is not None:
        fails.append("f) same-channel duplicates shouldn't trigger Rule 3 (need cross-channel)")
    return fails


def case_dupe_clear_after_trigger() -> list[str]:
    """After a trigger, caller calls clear() — subsequent posts of the same
    text in a 4th channel shouldn't re-trigger (it's a deduped pattern)."""
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "spam", now_s=100)
    t.add("u1", "ch_b", "m2", "spam", now_s=110)
    r1 = t.add("u1", "ch_c", "m3", "spam", now_s=120)
    if r1 is None:
        fails.append("g) trigger expected on 3rd channel")
    t.clear("u1", "spam")
    r2 = t.add("u1", "ch_d", "m4", "spam", now_s=130)
    if r2 is not None:
        fails.append("g) after clear, 1 channel post should not trigger")
    return fails


def case_dupe_gc_drops_expired() -> list[str]:
    fails = []
    t = bridge._DupeTracker(window_s=300, channel_threshold=3)
    t.add("u1", "ch_a", "m1", "spam", now_s=0)
    t.gc(now_s=400)
    if t._store:
        fails.append("h) gc should drop expired entries")
    return fails


# ---------------------------------------------------------------------------
# _OffTopicStreakTracker (Rule 7)
# ---------------------------------------------------------------------------

def case_offtopic_streak_below_threshold() -> list[str]:
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    # 4 off-topic msgs — under threshold
    fired = [t.record("ch1", f"u{i}", off_topic=True, is_mod=False, now_s=100 + i)
             for i in range(4)]
    if any(fired):
        fails.append("i) 4 off-topic should not fire (need 5)")
    return fails


def case_offtopic_streak_fires_at_5() -> list[str]:
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    fired_log = [t.record("ch1", f"u{i}", off_topic=True, is_mod=False, now_s=100 + i)
                 for i in range(5)]
    if not fired_log[-1]:
        fails.append("j) 5th off-topic should fire")
    return fails


def case_offtopic_on_topic_resets_streak() -> list[str]:
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    t.record("ch1", "u1", off_topic=True, is_mod=False, now_s=100)
    t.record("ch1", "u2", off_topic=True, is_mod=False, now_s=101)
    # On-topic interrupts streak — buffer keeps growing but the 5th won't
    # be all-off-topic, so no fire.
    t.record("ch1", "u3", off_topic=False, is_mod=False, now_s=102)
    t.record("ch1", "u4", off_topic=True, is_mod=False, now_s=103)
    t.record("ch1", "u5", off_topic=True, is_mod=False, now_s=104)
    fired = t.record("ch1", "u6", off_topic=True, is_mod=False, now_s=105)
    if fired:
        fails.append("k) on-topic msg in middle should prevent streak from firing")
    return fails


def case_offtopic_mod_resets_streak() -> list[str]:
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    for i in range(4):
        t.record("ch1", f"u{i}", off_topic=True, is_mod=False, now_s=100 + i)
    # Mod posts — clears the streak
    t.record("ch1", "mod1", off_topic=True, is_mod=True, now_s=104)
    # Now 4 more off-topic — needs 5 fresh, not 1 more
    fireds = [t.record("ch1", f"u{i+10}", off_topic=True, is_mod=False, now_s=200 + i)
              for i in range(4)]
    if any(fireds):
        fails.append("l) mod-reset should clear streak; next 4 shouldn't fire")
    fired_5 = t.record("ch1", "uX", off_topic=True, is_mod=False, now_s=210)
    if not fired_5:
        fails.append("l) 5 fresh post-mod-reset should fire")
    return fails


def case_offtopic_cooldown_suppresses() -> list[str]:
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    # First fire
    for i in range(5):
        t.record("ch1", f"u{i}", off_topic=True, is_mod=False, now_s=100 + i)
    # Try another 5 within cooldown — should NOT fire
    fireds = [t.record("ch1", f"u{i+10}", off_topic=True, is_mod=False, now_s=200 + i)
              for i in range(5)]
    if any(fireds):
        fails.append("m) within cooldown, second streak should not fire")
    # After cooldown
    fired_after = t.record("ch1", "uY", off_topic=True, is_mod=False, now_s=900)
    # Need to refill buffer; cooldown logic still keeps buffer trimmed during cooldown
    # so a single post post-cooldown isn't enough. Add 4 more.
    for i in range(4):
        fired_after = t.record("ch1", f"uZ{i}", off_topic=True, is_mod=False, now_s=901 + i)
    if not fired_after:
        fails.append("m) post-cooldown 5-streak should fire")
    return fails


def case_offtopic_per_channel_isolation() -> list[str]:
    """Streaks are per-channel — drift in #a doesn't trigger reminder in #b."""
    fails = []
    t = bridge._OffTopicStreakTracker(streak_len=5, cooldown_s=600)
    # Fill #a streak
    for i in range(5):
        t.record("ch_a", f"u{i}", off_topic=True, is_mod=False, now_s=100 + i)
    # #b should be unaffected
    fired_b = t.record("ch_b", "u_x", off_topic=True, is_mod=False, now_s=200)
    if fired_b:
        fails.append("n) per-channel isolation: ch_b shouldn't fire from ch_a streak")
    return fails


def main() -> int:
    cases = [
        ("a-norm", case_normalize_basic),
        ("b-dupe-below", case_dupe_below_threshold_no_trigger),
        ("c-dupe-trig", case_dupe_at_threshold_triggers),
        ("d-dupe-window", case_dupe_window_expiry),
        ("e-dupe-diff-text", case_dupe_different_text_separate_keys),
        ("f-dupe-same-ch", case_dupe_same_channel_doesnt_double_count),
        ("g-dupe-clear", case_dupe_clear_after_trigger),
        ("h-dupe-gc", case_dupe_gc_drops_expired),
        ("i-offtopic-below", case_offtopic_streak_below_threshold),
        ("j-offtopic-fires", case_offtopic_streak_fires_at_5),
        ("k-ontopic-reset", case_offtopic_on_topic_resets_streak),
        ("l-mod-reset", case_offtopic_mod_resets_streak),
        ("m-cooldown", case_offtopic_cooldown_suppresses),
        ("n-per-channel", case_offtopic_per_channel_isolation),
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
    print("\nAll mod-judge stateful-detector invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
