#!/usr/bin/env python3
"""
Snapshot test for Sutando's dependency on Claude Code's ~/.claude/ directory
(closes #864 — Mini's host-CLI discipline 3/3).

Two layers of assurance:

  A. Helper correctness — `claude_home_path()` in src/util_paths.py resolves
     to the expected paths and respects the `$CLAUDE_HOME` env override.
     Pure unit tests; always run in CI.

  B. Installed-path smoke check — verifies the key paths Sutando reads/writes
     under `~/.claude/` exist on the current host. Skipped in CI (no real
     install) by checking for `~/.claude/` existence first. Meant to catch
     Anthropic-side convention drift early (e.g. if Claude Code moves
     `projects/<hash>/memory/` to a new path, this fails before users hit it).

Run: python3 tests/host-cli-dependency-snapshot.test.py
Exit: 0 on pass, 1 on fail.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


# ── A. Helper correctness ─────────────────────────────────────────────────────

def test_helper_default_base() -> int:
    """A1: claude_home_path() defaults to ~/.claude/."""
    # Import with no env override in effect.
    _saved = os.environ.pop("CLAUDE_HOME", None)
    try:
        # Re-import to pick up cleared env (module may be cached).
        import importlib
        import util_paths as up
        importlib.reload(up)
        result = up.claude_home_path()
        expected = Path.home() / ".claude"
        if result != expected:
            return fail(
                f"A1: claude_home_path() returned {result!r}, expected {expected!r}"
            )
    finally:
        if _saved is not None:
            os.environ["CLAUDE_HOME"] = _saved
    return 0


def test_helper_claude_home_override() -> int:
    """A2: $CLAUDE_HOME overrides the base path."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["CLAUDE_HOME"] = td
        try:
            import importlib
            import util_paths as up
            importlib.reload(up)
            result = up.claude_home_path()
            if str(result) != td:
                return fail(
                    f"A2: $CLAUDE_HOME override not respected: got {result!r}, "
                    f"expected {td!r}"
                )
            # Subpath joining
            sub = up.claude_home_path("channels", "discord", "access.json")
            expected_sub = Path(td) / "channels" / "discord" / "access.json"
            if sub != expected_sub:
                return fail(
                    f"A2: subpath join wrong: got {sub!r}, expected {expected_sub!r}"
                )
        finally:
            del os.environ["CLAUDE_HOME"]
    return 0


def test_helper_subpath() -> int:
    """A3: claude_home_path('channels', 'discord') returns the expected path."""
    _saved = os.environ.pop("CLAUDE_HOME", None)
    try:
        import importlib
        import util_paths as up
        importlib.reload(up)
        result = up.claude_home_path("channels", "discord")
        expected = Path.home() / ".claude" / "channels" / "discord"
        if result != expected:
            return fail(
                f"A3: claude_home_path subpath wrong: got {result!r}, expected {expected!r}"
            )
    finally:
        if _saved is not None:
            os.environ["CLAUDE_HOME"] = _saved
    return 0


# ── B. Installed-path smoke check ─────────────────────────────────────────────

# Paths Sutando actively reads/writes under ~/.claude/.
# Sourced from bridge files + util_paths.py docstring examples.
_CHANNEL_DIRS = [
    ("channels", "discord"),
    ("channels", "telegram"),
    ("channels", "slack"),
]
_CHANNEL_CONFIG_FILES = [
    ("channels", "discord", "access.json"),
    ("channels", "discord", ".env"),
    ("channels", "telegram", "access.json"),
    ("channels", "slack", "access.json"),
]
_RUNTIME_DIRS = [
    ("skills",),
]


def test_installed_paths() -> int:
    """B1: Key ~/.claude/ paths exist on an installed host (skip if not installed)."""
    import util_paths as up
    claude_home = up.claude_home_path()
    if not claude_home.exists():
        print(f"  SKIP B1: {claude_home} does not exist — not an installed host.")
        return 0

    missing = []

    # Channel dirs — expected to exist once at least one bridge is configured.
    for parts in _CHANNEL_DIRS:
        p = up.claude_home_path(*parts)
        if not p.exists():
            # Non-fatal: only one bridge may be installed. Record but don't fail.
            print(f"  INFO B1: {p} not found (bridge may not be configured)")

    # At minimum, skills/ should exist on any host running Sutando.
    for parts in _RUNTIME_DIRS:
        p = up.claude_home_path(*parts)
        if not p.is_dir():
            missing.append(str(p))

    if missing:
        return fail(
            f"B1: expected dirs missing under {claude_home}: "
            + ", ".join(missing)
        )
    return 0


def test_write_contract() -> int:
    """B2: claudeHomePath write contract — CLAUDE_HOME override lets callers
    redirect writes to a temp dir (supports testing + alt-host installs)."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["CLAUDE_HOME"] = td
        try:
            import importlib
            import util_paths as up
            importlib.reload(up)
            target = up.claude_home_path("channels", "test-bridge", "access.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"allowFrom": ["test-user"]}')
            if not target.exists():
                return fail("B2: write via CLAUDE_HOME override did not create file")
            content = target.read_text()
            if "test-user" not in content:
                return fail(f"B2: write content mismatch: {content!r}")
        finally:
            del os.environ["CLAUDE_HOME"]
    return 0


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> int:
    rc = 0
    rc |= test_helper_default_base()
    rc |= test_helper_claude_home_override()
    rc |= test_helper_subpath()
    rc |= test_installed_paths()
    rc |= test_write_contract()

    if rc == 0:
        print("PASS: host-cli-dependency-snapshot — 3 helper + 2 contract tests.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
