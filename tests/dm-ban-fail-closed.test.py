#!/usr/bin/env python3
"""is_dm_banned() must fail closed: an unreadable sentinel parent is treated
as banned, never raises and never silently reads as unbanned."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from dm_ban import is_dm_banned  # noqa: E402


class IsDmBanned(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dm-ban-"))

    def test_no_sentinel_is_not_banned(self):
        (self.root / "state").mkdir()
        self.assertFalse(is_dm_banned(self.root))

    def test_sentinel_present_is_banned(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "dm-ban.sentinel").write_text("")
        self.assertTrue(is_dm_banned(self.root))

    def test_missing_state_dir_is_not_banned(self):
        self.assertFalse(is_dm_banned(self.root))

    def test_unreadable_state_dir_fails_closed(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permission bits")
        state_dir = self.root / "state"
        state_dir.mkdir()
        state_dir.chmod(0)
        try:
            self.assertTrue(is_dm_banned(self.root),
                             "an unresolvable sentinel must read as banned, not unbanned")
        finally:
            state_dir.chmod(stat.S_IRWXU)


if __name__ == "__main__":
    unittest.main(verbosity=2)
