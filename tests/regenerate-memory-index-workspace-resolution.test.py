#!/usr/bin/env python3
"""Regression guard for `skills/cross-node-sync/scripts/regenerate-memory-index.py`.

Historic bug: `default_memory_dir()` derived the Claude Code project slug from
`Path(__file__).resolve().parent` walked up four levels. When `skills/` is an
app-bundle symlink (the normal install layout), `.resolve()` follows it into
`/Applications/Sutando.app/Contents/Resources/repo` — so the memory dir pointed
somewhere with no frontmatter files, `collect_entries` returned 0 entries, and
the clobber guard turned the regen into a silent no-op on every
cross-node-sync cron pass.

Fix: derive the slug from `resolve_workspace()` (the canonical workspace
contract: $SUTANDO_WORKSPACE / ~/.sutando/workspace), never from __file__.
"""
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "cross-node-sync" / "scripts" / "regenerate-memory-index.py"


def _load():
    spec = importlib.util.spec_from_file_location("regen_memory_index", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_slugify_replaces_dots_and_slashes():
    m = _load()
    slug = m._slugify_path(Path("/Users/x/.sutando/workspace"))
    assert slug == "-Users-x--sutando-workspace", (
        f"slug must turn every non-alphanumeric char into '-' (incl. the dot "
        f"in '.sutando'); got {slug!r}"
    )


def test_memory_dir_honors_env_var():
    m = _load()
    os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/some-memory-dir"
    try:
        assert m.default_memory_dir() == Path("/tmp/some-memory-dir")
    finally:
        del os.environ["SUTANDO_MEMORY_DIR"]


def test_memory_dir_derived_from_workspace_not_script_path():
    """With no $SUTANDO_MEMORY_DIR, the memory dir must follow $SUTANDO_WORKSPACE
    — NOT the (possibly symlinked) location of the script file."""
    m = _load()
    os.environ.pop("SUTANDO_MEMORY_DIR", None)
    os.environ["SUTANDO_WORKSPACE"] = "/Users/x/.sutando/workspace"
    try:
        got = m.default_memory_dir()
        expected = Path.home() / ".claude" / "projects" / "-Users-x--sutando-workspace" / "memory"
        assert got == expected, f"expected {expected}, got {got}"
        # The bundle path is exactly what the old __file__-based code produced.
        assert "Sutando.app" not in str(got), (
            "memory dir resolved into the app bundle — the symlink-resolution "
            "regression has returned"
        )
    finally:
        del os.environ["SUTANDO_WORKSPACE"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
