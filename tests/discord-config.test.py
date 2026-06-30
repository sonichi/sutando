#!/usr/bin/env python3
"""Tests for src/discord_config.py — resolve_owner_id() resolution chain.

Covers all 5 resolution steps: env-var override, workspace owner field,
workspace tierMap, legacy access.json owner, legacy access.json tierMap,
and the None fall-through when no candidate is found.

Network: none.  File I/O: none (config injected via fixture param).

Run: python3 tests/discord-config.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Inject src/ so workspace_default import in discord_config resolves.
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location(
    "discord_config", REPO / "src" / "discord_config.py"
)
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# resolve_owner_id — step 1: env-var override
# ---------------------------------------------------------------------------

def test_env_var_wins_over_config() -> list[str]:
    """SUTANDO_DM_OWNER_ID env var wins over all config sources."""
    fails: list[str] = []
    access = {"allowFrom": ["999"], "owner": "999"}
    cfg = {"owner": "888"}
    with unittest.mock.patch.dict(os.environ, {"SUTANDO_DM_OWNER_ID": "777"}):
        result = dc.resolve_owner_id(access, config=cfg)
    check("env var value returned", result == "777", fails)
    return fails


def test_env_var_stripped() -> list[str]:
    """Whitespace in SUTANDO_DM_OWNER_ID is stripped."""
    fails: list[str] = []
    with unittest.mock.patch.dict(os.environ, {"SUTANDO_DM_OWNER_ID": "  123  "}):
        result = dc.resolve_owner_id({})
    check("env var stripped", result == "123", fails)
    return fails


def test_empty_env_var_not_returned() -> list[str]:
    """Empty SUTANDO_DM_OWNER_ID falls through to next step."""
    fails: list[str] = []
    cfg = {"owner": "456"}
    with unittest.mock.patch.dict(os.environ, {"SUTANDO_DM_OWNER_ID": ""}, clear=False):
        # Must remove it if set to empty — env patch dict doesn't unset
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            result = dc.resolve_owner_id({}, config=cfg)
    check("empty env var falls through to config owner", result == "456", fails)
    return fails


# ---------------------------------------------------------------------------
# resolve_owner_id — step 2: workspace owner field
# ---------------------------------------------------------------------------

def test_workspace_owner_field() -> list[str]:
    """config['owner'] is returned when env var is absent."""
    fails: list[str] = []
    cfg = {"owner": "111"}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id({}, config=cfg)
    check("workspace owner returned", result == "111", fails)
    return fails


def test_workspace_owner_trimmed() -> list[str]:
    """Whitespace in config['owner'] is stripped."""
    fails: list[str] = []
    cfg = {"owner": "  222  "}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id({}, config=cfg)
    check("workspace owner stripped", result == "222", fails)
    return fails


# ---------------------------------------------------------------------------
# resolve_owner_id — step 3: workspace tierMap
# ---------------------------------------------------------------------------

def test_workspace_tier_map_owner() -> list[str]:
    """Workspace tierMap uid tagged owner is returned (step 3)."""
    fails: list[str] = []
    cfg = {"tierMap": {"333": "owner", "444": "team"}}
    access = {"allowFrom": ["333", "444"]}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config=cfg)
    check("workspace tierMap owner returned", result == "333", fails)
    return fails


def test_workspace_tier_map_uid_must_be_in_allow_list() -> list[str]:
    """Workspace tierMap owner uid is only returned if it's in allowFrom."""
    fails: list[str] = []
    cfg = {"tierMap": {"555": "owner"}}
    access = {"allowFrom": ["666"]}  # 555 not in allowFrom
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config=cfg)
    check("uid not in allowFrom is skipped", result is None, fails)
    return fails


# ---------------------------------------------------------------------------
# resolve_owner_id — step 4: legacy access.json owner field
# ---------------------------------------------------------------------------

def test_legacy_access_owner_field() -> list[str]:
    """access_data['owner'] is returned when no workspace config (step 4)."""
    fails: list[str] = []
    access = {"allowFrom": ["777"], "owner": "777"}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config={})
    check("legacy access owner returned", result == "777", fails)
    return fails


# ---------------------------------------------------------------------------
# resolve_owner_id — step 5: legacy access.json tierMap
# ---------------------------------------------------------------------------

def test_legacy_access_tier_map() -> list[str]:
    """access_data['tierMap'] owner uid is returned (step 5)."""
    fails: list[str] = []
    access = {
        "allowFrom": ["888", "999"],
        "tierMap": {"888": "team", "999": "owner"},
    }
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config={})
    check("legacy tierMap owner returned", result == "999", fails)
    return fails


# ---------------------------------------------------------------------------
# resolve_owner_id — no candidate → None
# ---------------------------------------------------------------------------

def test_returns_none_when_no_candidate() -> list[str]:
    """Returns None when no owner can be resolved (callers must handle I/O fallback)."""
    fails: list[str] = []
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id({}, config={})
    check("no candidate → None", result is None, fails)
    return fails


def test_returns_none_with_team_only_tier_map() -> list[str]:
    """Only 'team' tier in tierMap → None (does not promote team to owner)."""
    fails: list[str] = []
    access = {
        "allowFrom": ["100"],
        "tierMap": {"100": "team"},
    }
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config={})
    check("team-only tierMap returns None", result is None, fails)
    return fails


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

def test_workspace_owner_wins_over_legacy() -> list[str]:
    """Workspace config['owner'] (step 2) wins over legacy access['owner'] (step 4)."""
    fails: list[str] = []
    cfg = {"owner": "WORKSPACE"}
    access = {"owner": "LEGACY", "allowFrom": ["LEGACY"]}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config=cfg)
    check("workspace owner wins over legacy", result == "WORKSPACE", fails)
    return fails


def test_workspace_tier_map_wins_over_legacy_owner() -> list[str]:
    """Workspace tierMap (step 3) wins over legacy access['owner'] (step 4)."""
    fails: list[str] = []
    cfg = {"tierMap": {"TIERED": "owner"}}
    access = {"allowFrom": ["TIERED"], "owner": "LEGACY"}
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DM_OWNER_ID"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = dc.resolve_owner_id(access, config=cfg)
    check("workspace tierMap wins over legacy owner field", result == "TIERED", fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("env_var: wins over all config sources", test_env_var_wins_over_config),
        ("env_var: whitespace stripped", test_env_var_stripped),
        ("env_var: empty falls through to config", test_empty_env_var_not_returned),
        ("workspace owner: returned when env absent", test_workspace_owner_field),
        ("workspace owner: whitespace stripped", test_workspace_owner_trimmed),
        ("workspace tierMap: owner uid in allowFrom returned", test_workspace_tier_map_owner),
        ("workspace tierMap: uid not in allowFrom skipped", test_workspace_tier_map_uid_must_be_in_allow_list),
        ("legacy access owner: returned when no workspace config", test_legacy_access_owner_field),
        ("legacy access tierMap: owner uid returned", test_legacy_access_tier_map),
        ("no candidate: returns None", test_returns_none_when_no_candidate),
        ("no candidate: team-only tierMap → None", test_returns_none_with_team_only_tier_map),
        ("priority: workspace owner wins over legacy", test_workspace_owner_wins_over_legacy),
        ("priority: workspace tierMap wins over legacy owner", test_workspace_tier_map_wins_over_legacy_owner),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\ndiscord-config: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
