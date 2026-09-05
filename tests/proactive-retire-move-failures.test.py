#!/usr/bin/env python3
"""Retirement never destroys bytes when the move or its verification fails.

`retire_claim_if_unchanged` moves a delivered claim into `retired/` and re-reads
the moved inode. Three failure paths must all end with the bytes still on disk
and False returned: the move cannot happen, the re-read cannot be trusted, and
the undo of a bad move itself fails.
"""
from __future__ import annotations

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.readiness import retire_claim_if_unchanged  # noqa: E402

BODY = "the delivered proactive body"


def _claim(td: str) -> Path:
    claim = Path(td) / "proactive-1788355000.sending"
    claim.write_text(BODY)
    return claim


def _raise_on(target: Path, exc: OSError):
    """Fail read_bytes for exactly one path; every other read stays real."""
    real = Path.read_bytes

    def fake(self):
        if self == target:
            raise exc
        return real(self)

    return patch.object(Path, "read_bytes", fake)


class RetireMoveFailureTest(unittest.TestCase):
    def test_an_unmovable_claim_is_released_with_its_bytes_intact(self):
        with tempfile.TemporaryDirectory() as td:
            claim = _claim(td)
            # A regular file where retired/ must be: mkdir raises, so can the move.
            (Path(td) / "retired").write_text("not a directory")
            self.assertFalse(retire_claim_if_unchanged(claim, BODY))
            self.assertTrue(claim.exists(), "claim was destroyed despite a failed move")
            self.assertEqual(claim.read_text(), BODY)

    def test_an_unverifiable_move_is_undone_and_released(self):
        with tempfile.TemporaryDirectory() as td:
            claim = _claim(td)
            retired = Path(td) / "retired" / claim.name
            denied = PermissionError(errno.EACCES, "Permission denied")
            with _raise_on(retired, denied):
                self.assertFalse(retire_claim_if_unchanged(claim, BODY))
            self.assertTrue(claim.exists(), "the unverifiable move was not undone")
            self.assertEqual(claim.read_text(), BODY)
            self.assertFalse(retired.exists())

    def test_a_failed_undo_still_refuses_and_keeps_the_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            claim = _claim(td)
            retired = Path(td) / "retired" / claim.name
            denied = PermissionError(errno.EACCES, "Permission denied")
            real_replace = Path.replace

            def fake_replace(self, target):
                if self == retired:
                    raise OSError(errno.EIO, "Input/output error")
                return real_replace(self, target)

            with _raise_on(retired, denied), patch.object(Path, "replace", fake_replace):
                self.assertFalse(retire_claim_if_unchanged(claim, BODY))
            # Undo failed, so the bytes live on under retired/ — destroyed nowhere.
            self.assertEqual(retired.read_text(), BODY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
