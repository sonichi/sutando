"""Tests for fleet-roster skill.

Key invariant: fleet_roster.py ships with ZERO IDs. IDs live in a private
per-host file (fleet-roster.local.json), never committed or memory-synced.
"""
import ast
import sys
import os
from pathlib import Path

SKILL_PY = Path(__file__).parents[1] / "skills" / "fleet-roster" / "scripts" / "fleet_roster.py"
src = SKILL_PY.read_text()


def test_no_hardcoded_ids():
    """No Discord snowflake IDs in the committed skill source."""
    import re
    # Discord IDs are 17-20 digit numbers; check no long numeric literals in source
    ids_found = re.findall(r'\b\d{17,20}\b', src)
    assert not ids_found, (
        f"Hardcoded Discord IDs found in fleet_roster.py: {ids_found}. "
        "IDs must live in the private roster file only."
    )


def test_private_roster_path_default():
    """Default roster path is ~/.claude/fleet-roster.local.json (not workspace)."""
    assert "fleet-roster.local.json" in src, (
        "Default roster path must be ~/.claude/fleet-roster.local.json "
        "(private, outside workspace and memory-sync)"
    )


def test_configurable_path():
    """FLEET_ROSTER_PATH env var overrides the roster file path."""
    assert "FLEET_ROSTER_PATH" in src


def test_raises_without_roster():
    """mention() raises FileNotFoundError when private roster not installed."""
    # Use a path that doesn't exist
    os.environ["FLEET_ROSTER_PATH"] = "/nonexistent/fleet-roster.local.json"
    sys.path.insert(0, str(SKILL_PY.parent))
    import importlib
    import fleet_roster
    importlib.reload(fleet_roster)
    try:
        fleet_roster.mention("pro")
        assert False, "should have raised"
    except FileNotFoundError:
        pass
    finally:
        del os.environ["FLEET_ROSTER_PATH"]


def test_mention_with_private_roster():
    """mention() works when private roster is installed."""
    import tempfile, json, importlib, fleet_roster
    roster = {"pro": {"id": "1509329143110565888", "role": "agent"}}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(roster, f)
        fname = f.name
    os.environ["FLEET_ROSTER_PATH"] = fname
    importlib.reload(fleet_roster)
    result = fleet_roster.mention("pro")
    assert result == "<@1509329143110565888>", f"got {result}"
    os.unlink(fname)
    del os.environ["FLEET_ROSTER_PATH"]


def test_platform_param():
    """mention() accepts platform param."""
    assert "platform" in src and "ag2.space" in src


def test_mention_verified_defined():
    """mention_verified() async function must exist."""
    assert "async def mention_verified" in src


def test_dependency_direction():
    """Skill imports discord-bridge, never reverse — documented in docstring."""
    assert "skill → discord-bridge" in src


def test_no_synced_data_file():
    """fleet-roster.json must NOT exist in workspace/data/."""
    workspace = Path.home() / ".sutando" / "workspace" / "data" / "fleet-roster.json"
    assert not workspace.exists(), (
        f"fleet-roster.json found at {workspace} — would be memory-synced. Delete it."
    )


if __name__ == "__main__":
    tests = [
        test_no_hardcoded_ids, test_private_roster_path_default, test_configurable_path,
        test_raises_without_roster, test_mention_with_private_roster,
        test_platform_param, test_mention_verified_defined, test_dependency_direction,
        test_no_synced_data_file,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__} — {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
