#!/usr/bin/env python3
"""Regression test for the relative-SUTANDO_WORKSPACE anchor.

A relative `SUTANDO_WORKSPACE` resolves against CWD-at-use-time.
Two processes inheriting the same env value (launchd-managed bridge
vs bare-shell watcher, for example) but running with different CWDs
would `mkdir`/read in different directories — silent split-brain.

This module's own docstring already documents the same anti-pattern
for the `Path(__file__).resolve().parent.parent` fallback. The fix
extends the same guard to the env-set branch.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ws = _load("ws_default", REPO / "src" / "workspace_default.py")


def _clear_env():
    return {k: os.environ.pop(k, None) for k in ("SUTANDO_WORKSPACE",)}


def _restore_env(snap):
    for k, v in snap.items():
        if v is not None:
            os.environ[k] = v


def test_relative_env_path_is_anchored_to_cwd_absolute():
    """Bug fix regression guard. A relative `SUTANDO_WORKSPACE` MUST
    NOT survive resolution — anchor to CWD-at-resolve-time so two
    processes (different CWDs) don't silently split-brain on the same
    env value."""
    snap = _clear_env()
    tmp = Path(tempfile.mkdtemp(prefix="sutando-ws-cwd-"))
    original_cwd = Path.cwd()
    os.chdir(tmp)
    os.environ["SUTANDO_WORKSPACE"] = "myws"  # deliberately relative
    try:
        result = ws.resolve_workspace(migrate=False)
        assert result.is_absolute(), (
            f"relative env survived as {result!r} — bug: a launchd-managed "
            "process and a bare-shell process would land in different dirs"
        )
        # Anchored ending should be the relative segment.
        assert result.name == "myws", f"expected anchored .../myws, got {result}"
    finally:
        os.chdir(original_cwd)
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def test_absolute_env_path_unchanged():
    """Backwards compat: absolute `SUTANDO_WORKSPACE` returned as-is."""
    snap = _clear_env()
    abs_path = Path(tempfile.mkdtemp(prefix="sutando-ws-abs-"))
    os.environ["SUTANDO_WORKSPACE"] = str(abs_path)
    try:
        result = ws.resolve_workspace(migrate=False)
        assert result == abs_path
    finally:
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def test_tilde_in_env_path_is_expanded():
    """`~` expansion preserved — existing contract."""
    snap = _clear_env()
    os.environ["SUTANDO_WORKSPACE"] = "~/sutando-tilde-test"
    try:
        result = ws.resolve_workspace(migrate=False)
        assert "~" not in str(result), f"tilde not expanded: {result}"
        assert str(result).startswith(str(Path.home()))
    finally:
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def main():
    test_relative_env_path_is_anchored_to_cwd_absolute()
    test_absolute_env_path_unchanged()
    test_tilde_in_env_path_is_expanded()
    print("All relative-env-anchor tests passed.")


if __name__ == "__main__":
    main()
