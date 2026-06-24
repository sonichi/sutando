"""Tests for list_channel_members in discord-bridge.

Tests the function signature and guild-members intent configuration.
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "discord-bridge.py"


def _read_src():
    return SRC.read_text()


def test_members_intent_enabled():
    """intents.members = True must be set for GUILD_MEMBERS privileged intent."""
    src = _read_src()
    assert "intents.members = True" in src, "intents.members must be True"


def test_list_channel_members_defined():
    """list_channel_members async function must exist."""
    src = _read_src()
    assert "async def list_channel_members" in src, "list_channel_members must be defined"


def test_list_channel_members_returns_list():
    """Function must return list of dicts with id, name, display_name, is_bot."""
    src = _read_src()
    tree = ast.parse(src)
    fn_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_channel_members":
            fn_found = True
            fn_src = ast.unparse(node)
            assert '"id"' in fn_src or "'id'" in fn_src, "must include 'id' in return dict"
            assert "is_bot" in fn_src, "must include is_bot in return dict"
    assert fn_found, "list_channel_members not found"


def test_guild_members_fetch():
    """Must use guild.fetch_members (works without caching all members in memory)."""
    src = _read_src()
    assert "fetch_members" in src, "must call guild.fetch_members"


if __name__ == "__main__":
    tests = [test_members_intent_enabled, test_list_channel_members_defined,
             test_list_channel_members_returns_list, test_guild_members_fetch]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__} — {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
