"""Opt a test's subprocesses in (or out) of macOS notifications.

Writes `notifications.macos` into the workspace config layer and points the loader at that
workspace through its test-mode seam, so the shipped reader decides, not a stub of it.
"""
from __future__ import annotations

import json
from pathlib import Path


def macos_opt_in_env(workspace, on: bool = True) -> dict:
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "sutando.config.local.json").write_text(json.dumps({"notifications": {"macos": on}}))
    return {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws)}
