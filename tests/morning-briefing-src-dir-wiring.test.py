#!/usr/bin/env python3
"""`_SRC_DIR` wiring: every sibling-script path must be built from it.

**This is a wiring test, NOT a regression test — and the distinction is the
point.** The expression it replaced (`Path(__file__).parent`) yields the same
absolute path on every interpreter this repo supports, because Python >=3.11
absolutises `__file__` (bpo-20443). A test asserting those paths are absolute
would therefore pass against both versions and prove nothing; PR #2898 shipped
exactly that test and it was correctly rejected.

What IS checkable is the wiring: each builder must READ `_SRC_DIR`, so that
redefining `_SRC_DIR` moves the path. That is what `briefing-all-clear-verified`
depends on when it stubs the module to an absent directory, and it is what
would break if a builder were reverted to `Path(__file__).parent` — the builder
would then find the real script and the assertions below would fail.

Control arm (run before committing): revert any one builder to
`Path(__file__).parent / "<script>"` and its case here fails.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mb_wiring", REPO / "src" / "morning-briefing.py")
mb = importlib.util.module_from_spec(_spec)
sys.modules["mb_wiring"] = mb
try:
    _spec.loader.exec_module(mb)
except SystemExit:
    pass

# A directory that cannot contain any sibling script. Each gather resolves its
# script under _SRC_DIR and returns None when it is absent, so redirecting
# _SRC_DIR here proves the builder read it rather than recomputing its own root.
ABSENT = Path("/nonexistent-sutando-src-wiring")


class SrcDirWiring(unittest.TestCase):
    def setUp(self):
        self._real = mb._SRC_DIR
        mb._SRC_DIR = ABSENT
        # get_daily_insight returns TODAY'S SENTINEL before it ever builds a
        # script path, so without this the builder below is never reached.
        self._real_state = mb.STATE_DIR
        mb.STATE_DIR = ABSENT

    def tearDown(self):
        mb._SRC_DIR = self._real
        mb.STATE_DIR = self._real_state

    def test_health_check_path_follows_src_dir(self):
        """`hc = _SRC_DIR / "health-check.py"` — reverting it finds the real script."""
        self.assertIsNone(mb.get_health_issues())

    def test_daily_insight_path_follows_src_dir(self):
        """Same builder, one gather down."""
        self.assertIsNone(mb.get_daily_insight())

    def test_reminders_path_follows_src_dir(self):
        """Uses `_SRC_DIR.parent` — the one builder that walks UP, so a wrong
        root here fails differently from the two above."""
        self.assertIsNone(mb.get_reminders())

    def test_src_dir_is_absolute_and_real(self):
        """Guards the definition itself: the stub above only means something if
        the real value is a genuine directory containing the module."""
        self.assertTrue(self._real.is_absolute())
        self.assertTrue((self._real / "morning-briefing.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
