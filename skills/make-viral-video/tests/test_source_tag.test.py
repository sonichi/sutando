#!/usr/bin/env python3
"""
Wrapper that runs the self-contained tests in scripts/test_source_tag.py
via unittest so they are discovered by the npm test:py glob.

All test logic lives in the source script; this file just imports it,
extracts the test functions, and delegates execution.

Run: python3 skills/make-viral-video/tests/test_source_tag.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
_spec = importlib.util.spec_from_file_location(
    "test_source_tag",
    REPO / "skills" / "make-viral-video" / "scripts" / "test_source_tag.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestSourceTagWrapper(unittest.TestCase):
    """Thin wrappers — each delegates to an existing test function in the module."""

    def test_empty_manifest(self):
        _mod.test_empty_manifest()

    def test_manifest_without_source_tag(self):
        _mod.test_manifest_without_source_tag()

    def test_single_event_full_chain(self):
        _mod.test_single_event_full_chain()

    def test_duplicate_footer_shorts_deduped(self):
        _mod.test_duplicate_footer_shorts_deduped()

    def test_multi_event_bloat_pattern(self):
        _mod.test_multi_event_bloat_pattern()

    def test_partial_source_tag_skipped(self):
        _mod.test_partial_source_tag_skipped()

    def test_license_gate_warns_on_needs_license(self):
        _mod.test_license_gate_warns_on_needs_license_no_attribution()

    def test_license_gate_silent_on_other_licenses(self):
        _mod.test_license_gate_silent_on_other_licenses()

    def test_source_tag_none_handled_safely(self):
        _mod.test_source_tag_none_handled_safely()


if __name__ == "__main__":
    unittest.main()
