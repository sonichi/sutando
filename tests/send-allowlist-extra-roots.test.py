#!/usr/bin/env python3
"""Canonical allowlist: extra_roots extension + fail-closed invariants.

Covers the shared-allowlist tidy: slack-bridge and telegram-bridge now delegate
to send_allowlist.is_path_sendable instead of keeping hand-written copies that
had drifted apart. Slack passes its inbound dir via extra_roots.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from policy.egress.attachment import is_path_sendable, SEND_ALLOWED_ROOTS


class ExtraRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="allow-test-"))
        self.extra = self.tmp / "inbox"; self.extra.mkdir()

    def _mk(self, p):
        p = Path(p); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("x"); return str(p)

    def test_regular_file_under_extra_root_is_accepted(self):
        f = self._mk(self.extra / "d.png")
        self.assertFalse(is_path_sendable(f), "must be denied without extra_roots")
        self.assertTrue(is_path_sendable(f, extra_roots=(str(self.extra),)))

    def test_extra_roots_does_not_leak_to_siblings(self):
        """An extra root grants only itself — not its parent or siblings."""
        sib = self._mk(self.tmp / "elsewhere" / "d.png")
        self.assertFalse(is_path_sendable(sib, extra_roots=(str(self.extra),)))

    def test_extra_root_still_requires_a_regular_file(self):
        self.assertFalse(is_path_sendable(str(self.extra), extra_roots=(str(self.extra),)))
        self.assertFalse(is_path_sendable(str(self.extra / "nope.png"),
                                          extra_roots=(str(self.extra),)))

    def test_symlink_escaping_an_extra_root_is_denied(self):
        """realpath boundary check: a link inside the root pointing outside it."""
        outside = self._mk(self.tmp / "secret.txt")
        link = self.extra / "escape.txt"; os.symlink(outside, link)
        self.assertFalse(is_path_sendable(str(link), extra_roots=(str(self.extra),)))

    def test_prefix_sibling_not_matched_as_root(self):
        """<root>-evil must not be accepted just because it shares a prefix."""
        evil = self._mk(Path(str(self.extra) + "-evil") / "d.png")
        self.assertFalse(is_path_sendable(evil, extra_roots=(str(self.extra),)))

    def test_default_policy_unchanged_when_extra_roots_omitted(self):
        """Delegating with no extras must equal the canonical roots exactly."""
        f = self._mk(self.extra / "d.png")
        self.assertEqual(is_path_sendable(f), is_path_sendable(f, extra_roots=()))
        self.assertTrue(any(r.endswith("results") for r in SEND_ALLOWED_ROOTS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
