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

Run the ONE-BUILDER revert, not the whole-file one — they fail for different
reasons and only one of them is evidence:

    revert the WHOLE src file       FAILED (errors=4)    AttributeError: _SRC_DIR
    revert ONE builder, keep it     FAILED (failures=1)  the exact builder

`errors=4` only proves a symbol is missing; any implementation defining
`_SRC_DIR` satisfies it. `failures=1` isolates the builder and is the real
assertion. A reader who runs the obvious whole-file revert will see the bigger
number and credit this file with more than it earns.
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
        self._real_state = mb.STATE_DIR
        mb.STATE_DIR = ABSENT

    def tearDown(self):
        mb._SRC_DIR = self._real
        mb.STATE_DIR = self._real_state

    def test_health_check_path_follows_src_dir(self):
        """`hc = _SRC_DIR / "health-check.py"` — reverting it finds the real script."""
        self.assertIsNone(mb.get_health_issues())

    def test_reminders_path_follows_src_dir(self):
        """Uses `_SRC_DIR.parent` — the one builder that walks UP, so a wrong
        root here fails differently from the two above.

        Asserts the SUBPROCESS is never reached: a reverted builder finds the real script and then
        returns None anyway when Reminders.app does not answer inside the 10s
        timeout, so `assertIsNone` passes with the builder reverted. (Verified:
        it did, taking 10.006s — the timeout, not the guard.)
        """
        calls = []
        real_run = mb.subprocess.run
        mb.subprocess.run = lambda *a, **k: calls.append(a) or real_run(*a, **k)
        try:
            self.assertIsNone(mb.get_reminders())
        finally:
            mb.subprocess.run = real_run
        self.assertEqual(calls, [], "builder resolved a real script — it ignored _SRC_DIR")

    def test_notifier_loader_path_follows_src_dir(self):
        """The fourth builder — `_load_notifier` at src:472.

        Found while verifying the control arm: reverting THIS one left the
        suite green, because nothing asserted it. The module docstring claims
        "every sibling-script path", so an uncovered builder is a gap in the
        claim, not just in coverage. `_load_notifier` execs the module it
        resolves, so a stubbed `_SRC_DIR` must make it raise rather than
        silently load the real script.
        """
        with self.assertRaises(Exception):
            mb._load_notifier()

    def test_src_dir_is_absolute_and_real(self):
        """Guards the definition itself: the stub above only means something if
        the real value is a genuine directory containing the module."""
        self.assertTrue(self._real.is_absolute())
        self.assertTrue((self._real / "morning-briefing.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
