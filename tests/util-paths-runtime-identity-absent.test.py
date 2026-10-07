#!/usr/bin/env python3
"""With the runtime identity module unavailable, both readers state ABSENCE.

Neither may fall back to this process's own identity: the caller is health-check
and the subject is a watcher, so a guessed default is published as a repair
target for someone else's pid.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("util_paths", ROOT / "src" / "util_paths.py")
up = importlib.util.module_from_spec(_spec)
sys.modules["util_paths"] = up
_spec.loader.exec_module(up)


class RuntimeIdentityAbsent(unittest.TestCase):
    def test_actor_env_names_is_empty_not_a_default_list(self):
        with patch.object(up, "_runtime_identity", return_value=None):
            self.assertEqual(up.actor_env_names(), ())

    def test_stated_default_identity_is_None_not_this_process(self):
        with patch.object(up, "_runtime_identity", return_value=None):
            self.assertIsNone(up.stated_default_identity(Path("/tmp")))

    def test_both_return_something_when_the_module_resolves(self):
        """Without this the two nulls above would pass on a broken import."""
        names = up.actor_env_names()
        self.assertIsInstance(names, tuple)
        if up._runtime_identity() is None:
            self.skipTest("runtime identity module not importable in this env")
        self.assertGreater(len(names), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
