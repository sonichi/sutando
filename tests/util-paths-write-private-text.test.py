#!/usr/bin/env python3
"""write_private_text() must produce a file that was NEVER group/other-readable.

The naive `write_text()` + `os.chmod(0o600)` pair passes any assertion made on
the FINAL mode — the file is 0600 by the time you look. It is only distinguishable
by removing the repair: with chmod/fchmod neutralised under a permissive umask,
only a file born 0600 can still be private.

If this test passes against a write-then-chmod implementation, it is not testing
anything (verified below: the control raises).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("util_paths", REPO / "src" / "util_paths.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestWritePrivateText(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self._umask = os.umask(0o000)          # maximally permissive: 0644 would become 0666

    def tearDown(self):
        os.umask(self._umask)

    def test_born_private_with_repairs_disabled(self):
        """The discriminator. chmod/fchmod are no-ops, so the mode on disk is the
        mode the file was CREATED with. write_text()+chmod would leave 0666."""
        target = self.tmp / "access.json"
        real_chmod, real_fchmod = os.chmod, os.fchmod
        os.chmod = lambda *a, **k: None
        os.fchmod = lambda *a, **k: None
        try:
            self.mod.write_private_text(target, '{"allowFrom": ["U123"]}\n')
        finally:
            os.chmod, os.fchmod = real_chmod, real_fchmod
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertFalse(mode & (stat.S_IRGRP | stat.S_IROTH),
                         f"born group/other-readable: {oct(mode)}")

    def test_control_write_then_chmod_would_fail_this(self):
        """Proves the assertion above can say NO. Same conditions, the OLD
        pattern — it must come out world-readable, or the test is vacuous."""
        target = self.tmp / "naive.json"
        real_chmod = os.chmod
        os.chmod = lambda *a, **k: None
        try:
            target.write_text("x")             # born at umask 0o000 -> 0666
            os.chmod(target, 0o600)            # neutralised
        finally:
            os.chmod = real_chmod
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertTrue(mode & (stat.S_IRGRP | stat.S_IROTH),
                        "control did not reproduce the window; test is vacuous")


    def test_hardening_failure_does_not_destroy_existing_content(self):
        """#2356 review: a permission-hardening failure must not empty the file.

        O_TRUNC empties at OPEN, so `O_CREAT|O_TRUNC` then fchmod would leave an
        existing durable backup EMPTY when hardening fails — destroying exactly
        the copy that exists to survive a wipe. Harden first, truncate after.
        """
        target = self.tmp / "access-backup.json"
        original = '{"allowFrom": ["UOWNER"], "tofuOwner": "UOWNER"}'
        target.write_text(original)

        real_fchmod = os.fchmod

        def boom(*a, **k):
            raise PermissionError("hardening failed")

        os.fchmod = boom
        try:
            with self.assertRaises(PermissionError):
                self.mod.write_private_text(target, '{"allowFrom": ["UNEW"]}')
        finally:
            os.fchmod = real_fchmod

        self.assertEqual(target.read_text(), original,
                         "existing backup was modified despite a hardening failure")

    def test_hardening_failure_leaves_no_leaked_fd(self):
        """The error path closes the descriptor itself (fdopen never took it)."""
        target = self.tmp / "fdcheck.json"
        target.write_text("x")
        real_fchmod = os.fchmod
        os.fchmod = lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope"))
        try:
            for _ in range(64):          # would exhaust a small fd table if leaked
                with self.assertRaises(PermissionError):
                    self.mod.write_private_text(target, "y")
        finally:
            os.fchmod = real_fchmod
        self.assertEqual(target.read_text(), "x")

    def test_content_and_overwrite(self):
        """Behaviour must be unchanged: content correct, and an existing file is
        truncated (not appended) and re-restricted even though the mode arg to
        os.open is ignored for an existing file."""
        target = self.tmp / "twice.json"
        target.write_text("aaaaaaaaaaaaaaaaaaaa")
        os.chmod(target, 0o666)
        self.mod.write_private_text(target, "bb")
        self.assertEqual(target.read_text(), "bb")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
