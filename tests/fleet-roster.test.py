"""Tests for fleet-roster skill.

Verifies: mention(), mention_verified() signature, no synced data file,
configurable path, platform param.
"""
import ast
import sys
from pathlib import Path

SKILL_PY = Path(__file__).parents[1] / "skills" / "fleet-roster" / "scripts" / "fleet_roster.py"

# Insert skill path for import
sys.path.insert(0, str(SKILL_PY.parent))
from fleet_roster import mention, get_member, list_members, _BUILTIN_ROSTER


def test_mention_discord():
    """mention('pro') returns correct Discord mention string."""
    assert mention("pro") == "<@1509329143110565888>"
    assert mention("mini") == "<@1490412828065267872>"
    assert mention("air") == "<@1485364006297534584>"
    assert mention("lucy") == "<@1494435872949665953>"


def test_mention_platform_param():
    """mention() accepts platform param; ag2.space returns @name placeholder."""
    assert mention("pro", platform="discord") == "<@1509329143110565888>"
    assert mention("pro", platform="ag2.space") == "@pro"


def test_mention_unknown_raises():
    """mention() raises ValueError for unknown names."""
    try:
        mention("unknown_bot")
        assert False, "should have raised"
    except ValueError:
        pass


def test_no_synced_data_file():
    """fleet-roster.json must NOT exist in workspace/data/ (IDs stay private)."""
    workspace = Path.home() / ".sutando" / "workspace" / "data" / "fleet-roster.json"
    assert not workspace.exists(), (
        f"fleet-roster.json found at {workspace} — IDs would be synced. "
        "Delete this file; IDs belong in the builtin dict only."
    )


def test_configurable_path():
    """FLEET_ROSTER_PATH env var overrides the roster file path."""
    src = SKILL_PY.read_text()
    assert "FLEET_ROSTER_PATH" in src, "must support FLEET_ROSTER_PATH env override"


def test_mention_verified_defined():
    """mention_verified() async function must exist."""
    src = SKILL_PY.read_text()
    assert "async def mention_verified" in src


def test_dependency_direction():
    """Skill imports discord-bridge, never the reverse — check docstring."""
    src = SKILL_PY.read_text()
    assert "skill → discord-bridge" in src, "must document dependency direction"


def test_builtin_roster_complete():
    """Builtin roster must contain all 4 core fleet members."""
    for name in ["air", "mini", "pro", "lucy"]:
        assert name in _BUILTIN_ROSTER, f"'{name}' missing from builtin roster"
        assert "id" in _BUILTIN_ROSTER[name]


if __name__ == "__main__":
    tests = [
        test_mention_discord, test_mention_platform_param, test_mention_unknown_raises,
        test_no_synced_data_file, test_configurable_path, test_mention_verified_defined,
        test_dependency_direction, test_builtin_roster_complete,
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
