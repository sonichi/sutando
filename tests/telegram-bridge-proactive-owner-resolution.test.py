#!/usr/bin/env python3
"""Unit tests for `_resolve_proactive_owner_id` in src/telegram-bridge.py.

The pre-fix code resolved the proactive-DM recipient via
`next(iter(load_allowed()))`. `load_allowed()` returns a `set`, so the
iteration order was hash-slot order, not list order. With a single
user in `allowFrom` (the common case) this happened to work; with
multiple users — e.g. after `/telegram:access allow` added a second
sender — it could route owner-notifications to the wrong human, the
same bug class that hits discord-bridge's poll_proactive when its
ad-hoc resolver iterates a set instead of the allowFrom list.

The fix extracts the resolution into `_resolve_proactive_owner_id`, a
pure function with a documented priority chain:

  1. $SUTANDO_DM_OWNER_ID env override.
  2. tierMap[uid] == "owner" — explicit admin tier signal.
  3. tofuOwner — first-install record from `tofu_onboard`. Only
     honored if still present in allowFrom (removed → admin opt-out).
  4. First entry in allowFrom IN LIST ORDER (not set order).

This file pins every branch of that chain.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-telegram-resolve-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("tbridge", REPO / "src" / "telegram-bridge.py")
resolve = bridge._resolve_proactive_owner_id


def test_env_override_beats_everything():
    """Priority 1: env beats tierMap, tofuOwner, list-order."""
    access = {
        "allowFrom": ["A", "B"],
        "tierMap": {"B": "owner"},
        "tofuOwner": "B",
    }
    assert resolve("override-id", access) == "override-id"


def test_env_override_empty_string_falls_through():
    """Empty `""` is the unset signal — `os.environ.get(..., "").strip()`
    returns `""` when the var isn't set. Must NOT short-circuit to `""`;
    the chain must continue to the next priority."""
    access = {"allowFrom": ["A", "B"]}
    assert resolve("", access) == "A"
    assert resolve(None, access) == "A"


def test_tier_map_owner_wins_over_list_order():
    """Priority 2: tierMap is the explicit admin signal."""
    access = {
        "allowFrom": ["first-id", "owner-id"],
        "tierMap": {"owner-id": "owner"},
    }
    assert resolve(None, access) == "owner-id"


def test_tier_map_uses_first_owner_entry_when_multiple():
    """Pathological multi-owner tier map → deterministic by list order."""
    access = {
        "allowFrom": ["A", "B", "C"],
        "tierMap": {"B": "owner", "C": "owner"},
    }
    assert resolve(None, access) == "B"


def test_tofu_owner_used_when_tier_map_absent():
    """Priority 3: standard first-install path."""
    access = {
        "allowFrom": ["A", "tofu-owner-id"],
        "tofuOwner": "tofu-owner-id",
    }
    assert resolve(None, access) == "tofu-owner-id"


def test_tofu_owner_ignored_when_removed_from_allowfrom():
    """Stale `tofuOwner` after `/telegram:access remove` — admin
    opt-out must be respected, not silently re-routed."""
    access = {
        "allowFrom": ["A", "B"],
        "tofuOwner": "delisted-id",
    }
    assert resolve(None, access) == "A"


def test_tier_map_beats_tofu_owner():
    """tierMap > tofuOwner when both disagree."""
    access = {
        "allowFrom": ["A", "tier-owner", "tofu-owner"],
        "tierMap": {"tier-owner": "owner"},
        "tofuOwner": "tofu-owner",
    }
    assert resolve(None, access) == "tier-owner"


def test_first_in_allow_list_is_fallback():
    """Priority 4: list[0] — this is the actual bug fix."""
    access = {"allowFrom": ["first-owner", "second-user"]}
    assert resolve(None, access) == "first-owner"


def test_list_order_is_authoritative_under_permutation():
    """Same IDs, different list order → different result. Pins that a
    future refactor that hashes allowFrom into a set (the original bug)
    fails this test."""
    a_first = {"allowFrom": ["A", "B"]}
    b_first = {"allowFrom": ["B", "A"]}
    assert resolve(None, a_first) == "A"
    assert resolve(None, b_first) == "B"


def test_empty_allow_list_returns_none():
    """No eligible recipient → None. Caller MUST handle this — the
    main-loop logs `[proactive] no owner in allowFrom` and skips."""
    assert resolve(None, {"allowFrom": []}) is None
    assert resolve(None, {}) is None
    assert resolve("", {"allowFrom": []}) is None


def test_missing_tier_map_does_not_raise():
    """`tierMap` is optional — older access.json files predate it."""
    access = {"allowFrom": ["A", "B"]}
    assert resolve(None, access) == "A"


def test_tier_owner_must_appear_in_allow_list():
    """Defensive: a tier-tagged user not in allowFrom is ignored."""
    access = {
        "allowFrom": ["A", "B"],
        "tierMap": {"ghost-id": "owner"},
    }
    assert resolve(None, access) == "A"


def main():
    test_env_override_beats_everything()
    test_env_override_empty_string_falls_through()
    test_tier_map_owner_wins_over_list_order()
    test_tier_map_uses_first_owner_entry_when_multiple()
    test_tofu_owner_used_when_tier_map_absent()
    test_tofu_owner_ignored_when_removed_from_allowfrom()
    test_tier_map_beats_tofu_owner()
    test_first_in_allow_list_is_fallback()
    test_list_order_is_authoritative_under_permutation()
    test_empty_allow_list_returns_none()
    test_missing_tier_map_does_not_raise()
    test_tier_owner_must_appear_in_allow_list()
    print("All proactive-owner-resolution tests passed.")


if __name__ == "__main__":
    main()
