#!/usr/bin/env python3
"""Tests for archive-stale-results.py config resolution.

RETENTION_HOURS and DRY_RUN are module-level constants resolved via
config_get() (issue #1724 migration — config stanza first, then env). These
tests load the module fresh under controlled env to prove the resolution +
DRY_RUN's case-insensitive truthiness parse, and to exercise the config_get
call sites (which were otherwise uncovered by any test).
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SCRIPT = SRC / "archive-stale-results.py"
# The module's top-level `from sutando_config import config_get` needs src/ on
# sys.path (mirrors how it resolves when run as a script — script dir is path[0]).
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_fresh():
    """Re-import the module so its module-level constants re-evaluate under the
    currently-patched os.environ."""
    sys.modules.pop("archive_stale_results", None)
    spec = importlib.util.spec_from_file_location("archive_stale_results", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env_without(*keys):
    return {k: v for k, v in os.environ.items() if k not in keys}


class TestArchiveStaleResultsConfig(unittest.TestCase):
    def test_defaults(self):
        base = _env_without("RETENTION_HOURS", "DRY_RUN")
        with patch.dict(os.environ, base, clear=True):
            mod = _load_fresh()
        self.assertEqual(mod.RETENTION_HOURS, 24)
        self.assertFalse(mod.DRY_RUN)

    def test_retention_hours_override(self):
        base = _env_without("RETENTION_HOURS", "DRY_RUN")
        base["RETENTION_HOURS"] = "48"
        with patch.dict(os.environ, base, clear=True):
            mod = _load_fresh()
        self.assertEqual(mod.RETENTION_HOURS, 48)

    def test_dry_run_truthy(self):
        base = _env_without("RETENTION_HOURS", "DRY_RUN")
        base["DRY_RUN"] = "1"
        with patch.dict(os.environ, base, clear=True):
            mod = _load_fresh()
        self.assertTrue(mod.DRY_RUN)

    def test_dry_run_case_insensitive_false(self):
        # "No"/"FALSE" must parse as NOT dry-run (the .lower() guard, #354).
        for falsey in ("No", "FALSE", "false", "0", ""):
            base = _env_without("RETENTION_HOURS", "DRY_RUN")
            base["DRY_RUN"] = falsey
            with patch.dict(os.environ, base, clear=True):
                mod = _load_fresh()
            self.assertFalse(mod.DRY_RUN, f"DRY_RUN={falsey!r} should be falsey")


if __name__ == "__main__":
    unittest.main()
