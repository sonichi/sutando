#!/usr/bin/env python3
"""Regression test: health-check.py's MEMORY_DIR must stay consistent with
every other consumer of SUTANDO_MEMORY_DIR (src/voice-agent.ts,
src/voice-context.ts, CLAUDE.md/AGENTS.md all honor it as authoritative for
core memory) — this check must not silently diverge to a different
directory than the one actually read/written at runtime.

A leftover pre-#1454 SUTANDO_MEMORY_DIR value (see _default_memory_dir()'s
own docstring) can still point at a stale/defunct directory — that's flagged
via a separate memory-dir-override warn check instead of by redirecting
MEMORY_DIR itself out from under the rest of the runtime.

Run: python3 tests/health-check-memory-dir-override.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from util_paths import claude_project_slug  # noqa: E402


def _load_health_check():
    """Fresh import so module-level MEMORY_DIR picks up current env vars."""
    spec = importlib.util.spec_from_file_location(
        "health_check_memdir_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestMemoryDirHonorsOverride(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("SUTANDO_MEMORY_DIR", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = self._saved

    def test_memory_dir_matches_computed_default_when_env_unset(self):
        hc = _load_health_check()
        self.assertEqual(hc.MEMORY_DIR, Path(hc._default_memory_dir()))

    def test_default_memory_dir_uses_shared_slug_helper(self):
        """_default_memory_dir() must derive its slug through the shared
        claude_project_slug() helper, not a hand-rolled "/" -> "-" regex —
        the hand-rolled version silently resolved to a nonexistent dir on
        any checkout path containing a space or dot. The composition now
        lives in util_paths.default_memory_dir(); this pins the result, so it
        holds wherever the composition sits."""
        hc = _load_health_check()
        repo = Path(hc.__file__).parent.parent.resolve()
        expected_slug = claude_project_slug(repo)
        self.assertEqual(
            Path(hc._default_memory_dir()).parent.name, expected_slug)

    def test_env_override_is_honored_consistent_with_rest_of_runtime(self):
        """MEMORY_DIR must follow SUTANDO_MEMORY_DIR when set — matching
        voice-agent.ts / voice-context.ts, which also honor it. Silently
        diverging here would mean this check reports on a different
        directory than the one actually used at runtime."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SUTANDO_MEMORY_DIR"] = tmp
            hc = _load_health_check()
            self.assertEqual(hc.MEMORY_DIR, Path(tmp))


class TestMemoryDirOverrideCheck(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("SUTANDO_MEMORY_DIR", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = self._saved

    def test_no_check_when_env_unset(self):
        hc = _load_health_check()
        self.assertIsNone(hc.check_memory_dir_override())

    def test_no_check_when_override_matches_default(self):
        hc = _load_health_check()
        os.environ["SUTANDO_MEMORY_DIR"] = hc._default_memory_dir()
        self.assertIsNone(hc.check_memory_dir_override())

    def test_warns_when_override_diverges_from_default(self):
        """A stale pre-#1454 override (or any divergence) should surface a
        warn check with both paths — not silently redirect MEMORY_DIR."""
        with tempfile.TemporaryDirectory() as tmp:
            hc = _load_health_check()
            os.environ["SUTANDO_MEMORY_DIR"] = tmp
            result = hc.check_memory_dir_override()
            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "memory-dir-override")
            self.assertEqual(result["status"], "warn")
            self.assertIn(tmp, result["detail"])
            self.assertIn(hc._default_memory_dir(), result["detail"])


if __name__ == "__main__":
    unittest.main()
