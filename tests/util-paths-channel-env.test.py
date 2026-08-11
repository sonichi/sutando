#!/usr/bin/env python3
"""channel_env_path() must prefer state/auth/ but never strand a live token.

The move exists because $CLAUDE_CONFIG_DIR sits INSIDE the Team capability root,
so a Team task with Bash can read channels/<src>/.env. The risk in fixing it is
the opposite failure: a resolver that points at the new location before the file
is physically there would take every bridge offline. So the legacy fallback is
the load-bearing half, and it is what this pins.

The canonical-preference assertion is only meaningful if the legacy file also
exists at the same time — otherwise it passes for a resolver that always returns
the canonical path and never falls back at all.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(workspace: Path, ccd: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("util_paths", REPO / "src" / "util_paths.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod._workspace_root = lambda: workspace  # type: ignore[attr-defined]
    return mod


class TestChannelEnvPath(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.workspace = root / "workspace"
        self.ccd = root / "ccd"
        (self.workspace / "state" / "auth" / "channels" / "slack").mkdir(parents=True)
        (self.ccd / "channels" / "slack").mkdir(parents=True)
        self._prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.ccd)
        os.environ["SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER"] = "1"
        self.mod = _load(self.workspace, self.ccd)
        self.canonical = self.workspace / "state" / "auth" / "channels" / "slack" / ".env"
        self.legacy = self.ccd / "channels" / "slack" / ".env"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._prev
        self._td.cleanup()

    def test_falls_back_to_legacy_so_a_live_bridge_keeps_its_token(self) -> None:
        """Pre-migration: only the legacy file exists. Losing this strands the bridge."""
        self.legacy.write_text("SLACK_BOT_TOKEN=live\n")
        err = io.StringIO()
        with redirect_stderr(err):
            got = self.mod.channel_env_path("slack")
        self.assertEqual(got, self.legacy)
        self.assertIn("DEPRECATION", err.getvalue())
        self.assertIn("Team capability root", err.getvalue())

    def test_prefers_canonical_even_while_the_legacy_file_still_exists(self) -> None:
        """Both present — the post-copy, pre-delete window. Must pick state/auth/."""
        self.legacy.write_text("SLACK_BOT_TOKEN=stale\n")
        self.canonical.write_text("SLACK_BOT_TOKEN=migrated\n")
        err = io.StringIO()
        with redirect_stderr(err):
            got = self.mod.channel_env_path("slack")
        self.assertEqual(got, self.canonical)
        self.assertNotIn("DEPRECATION", err.getvalue())

    def test_returns_canonical_when_neither_exists_so_writers_land_in_state_auth(self) -> None:
        got = self.mod.channel_env_path("slack")
        self.assertEqual(got, self.canonical)

    def test_canonical_is_under_state_auth_not_the_config_dir(self) -> None:
        """The whole point: the resolved home must be outside $CLAUDE_CONFIG_DIR."""
        got = self.mod.channel_env_path("slack")
        self.assertNotIn(str(self.ccd), str(got))
        self.assertIn(os.path.join("state", "auth"), str(got))


if __name__ == "__main__":
    unittest.main(verbosity=2)
