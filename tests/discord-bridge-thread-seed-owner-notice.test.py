#!/usr/bin/env python3
"""
Tests for the thread-seed owner-visibility notice in discord-bridge.py.

When the bridge auto-seeds a thread into access.json (the #1498 path), a
non-owner can quietly accumulate sandboxed replies the owner never sees. The
fix posts an inline @-mention to the owner on first seed — but ONLY when a
non-owner did the seeding (owner-seeded threads need no ping).

Helpers under test (pure / testable):
  - _should_notify_owner_on_seed(sender_id, owner_ids): gating predicate
  - _format_seed_notice(owner_id, author_mention, parent_label, thread_id): body

Run: python3 tests/discord-bridge-thread-seed-owner-notice.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
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
_discord_stub.DMChannel = type("_DMChannel", (), {})
_discord_stub.Thread = type("_Thread", (), {})
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


# ---------------------------------------------------------------------------
# _should_notify_owner_on_seed
# ---------------------------------------------------------------------------

def case_non_owner_seeder_notifies() -> list[str]:
    fails = []
    # Non-owner seeds (sender not in owner list) → notify.
    if not bridge._should_notify_owner_on_seed("999", ["111", "222"]):
        fails.append("a) non-owner seeder should notify")
    # int sender id vs str owner ids → still notifies (type-agnostic compare).
    if not bridge._should_notify_owner_on_seed(999, ["111"]):
        fails.append("a) int sender_id vs str owner_ids should still notify")
    return fails


def case_owner_seeder_skips() -> list[str]:
    fails = []
    # Owner seeds their own thread → no ping (str match).
    if bridge._should_notify_owner_on_seed("111", ["111", "222"]):
        fails.append("b) owner seeder should NOT notify")
    # int owner id in list, str sender → coerced match, no ping.
    if bridge._should_notify_owner_on_seed("111", [111, 222]):
        fails.append("b) int owner_ids should coerce-match str sender")
    # int sender matching int owner → no ping.
    if bridge._should_notify_owner_on_seed(111, [111]):
        fails.append("b) int sender matching int owner should NOT notify")
    return fails


def case_no_owner_never_notifies() -> list[str]:
    fails = []
    # No owner to mention → never notify (avoid <@> with empty id).
    if bridge._should_notify_owner_on_seed("999", []):
        fails.append("c) empty owner list should NOT notify")
    if bridge._should_notify_owner_on_seed("999", None):
        fails.append("c) None owner list should NOT notify")
    return fails


# ---------------------------------------------------------------------------
# _format_seed_notice
# ---------------------------------------------------------------------------

def case_notice_body() -> list[str]:
    fails = []
    body = bridge._format_seed_notice("111", "<@999>", "#general", "555")
    # @-mention is what reaches the owner's client — must be present.
    if "<@111>" not in body:
        fails.append("d) notice must @-mention the owner")
    if "<@999>" not in body:
        fails.append("d) notice should name the seeding author")
    if "#general" not in body:
        fails.append("d) notice should name the parent channel")
    # The undo affordance must reference the seeded thread id.
    if "group rm 555" not in body:
        fails.append("d) notice should include the group-rm undo for the thread id")
    return fails


def main() -> int:
    cases = [
        ("a-non-owner", case_non_owner_seeder_notifies),
        ("b-owner-skip", case_owner_seeder_skips),
        ("c-no-owner", case_no_owner_never_notifies),
        ("d-body", case_notice_body),
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
    print("\nAll thread-seed owner-notice invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
