#!/usr/bin/env python3
"""The notify-cooldown stamp belongs under state/, not the workspace root; pins the
move, the legacy-stamp retirement, and the deliberate absence of a read-fallback."""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(_spec)
sys.modules["cpq"] = cpq
try:
    _spec.loader.exec_module(cpq)
except SystemExit:
    pass


class TestStampLocation(unittest.TestCase):
    def test_the_stamp_lives_under_state(self):
        self.assertEqual(cpq.LAST_NOTIFY_FILE.parent, cpq.WORKSPACE / "state")

    def test_it_is_NOT_at_the_workspace_root(self):
        self.assertNotEqual(cpq.LAST_NOTIFY_FILE.parent, cpq.WORKSPACE)
        self.assertNotEqual(cpq.LAST_NOTIFY_FILE.name, ".last-pq-notify")

    def test_health_check_would_not_call_the_new_location_drift(self):
        """Asserts allowlist MEMBERSHIP only, never where the probe scans; the
        root-tidy tests below drive the shipped probe for that."""
        hc_path = REPO / "src" / "health-check.py"
        if not hc_path.is_file():
            self.skipTest("health-check.py not present")
        s = importlib.util.spec_from_file_location("hc", hc_path)
        hc = importlib.util.module_from_spec(s)
        sys.modules["hc"] = hc
        try:
            s.loader.exec_module(hc)
        except SystemExit:
            pass
        # The old name was drift; that is WHY this moved. Guards against someone
        # "fixing" the warning by widening the allowlist instead.
        self.assertNotIn(".last-pq-notify", hc.WORKSPACE_ROOT_ALLOWED,
                         "the root file was drift — do not sanction it, move the writer")


class TestWriteNotifyStamp(unittest.TestCase):
    """Exercises the real function, not a re-implementation of its two lines."""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp())
        self._saved = cpq.LAST_NOTIFY_FILE
        cpq.LAST_NOTIFY_FILE = self.ws / "state" / "last-pq-notify"

    def tearDown(self):
        cpq.LAST_NOTIFY_FILE = self._saved

    def test_it_creates_state_when_absent(self):
        # A fresh workspace has no state/ until something makes it; without the
        # mkdir this raises FileNotFoundError and the cooldown never records.
        self.assertFalse(cpq.LAST_NOTIFY_FILE.parent.exists(), "precondition")
        cpq.write_notify_stamp([], now=1700000000)
        self.assertTrue(cpq.LAST_NOTIFY_FILE.is_file())

    def test_it_records_timestamp_and_set_key(self):
        cpq.write_notify_stamp([], now=1700000000)
        ts, key = cpq.LAST_NOTIFY_FILE.read_text().split()
        self.assertEqual(ts, "1700000000")
        self.assertEqual(key, cpq.notify_key([]), "the key must be the reader's key")

    def test_the_stamp_records_the_key_the_cooldown_compares(self):
        # Empty input cannot catch a writer pointed at the wrong function: pick a
        # set where notify_key and questions_key provably differ, then assert both.
        qs = [{"title": t} for t in ("a", "b", "c")]
        self.assertNotEqual(cpq.notify_key(qs), cpq.questions_key(qs), "precondition")
        cpq.write_notify_stamp(qs, now=1700000001)
        _, key = cpq.LAST_NOTIFY_FILE.read_text().split()
        self.assertEqual(key, cpq.notify_key(qs))
        self.assertNotEqual(key, cpq.questions_key(qs))

    def test_it_is_idempotent_on_an_existing_state_dir(self):
        cpq.write_notify_stamp([], now=1)
        cpq.write_notify_stamp([], now=2)
        self.assertTrue(cpq.LAST_NOTIFY_FILE.read_text().startswith("2 "))

    def test_the_notify_flow_delegates_to_it(self):
        # Guard the wiring: two inline lines could drift back in and this file's
        # location guarantee would then only cover the constant.
        src = (REPO / "src" / "check-pending-questions.py").read_text()
        self.assertIn("write_notify_stamp(questions)", src)
        self.assertEqual(src.count("LAST_NOTIFY_FILE.write_text("), 1,
                         "exactly one writer, inside write_notify_stamp")


class TestUpgradedWorkspaceIsCleanedUp(unittest.TestCase):
    """An install that already has the root file must end up clean; retirement runs
    AFTER the new stamp is written, so a crash costs one cooldown."""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp())
        self.legacy = self.ws / ".last-pq-notify"
        self._saved = cpq.LAST_NOTIFY_FILE
        cpq.LAST_NOTIFY_FILE = self.ws / "state" / "last-pq-notify"

    def tearDown(self):
        cpq.LAST_NOTIFY_FILE = self._saved

    def test_the_old_root_stamp_is_retired(self):
        self.legacy.write_text("1785876643 2f35a3c6")
        cpq.write_notify_stamp([], now=1700000000)
        self.assertTrue(cpq.LAST_NOTIFY_FILE.is_file(), "new stamp must exist")
        self.assertFalse(self.legacy.exists(), "the drift the probe flags must be gone")

    def test_it_is_written_BEFORE_the_old_one_is_removed(self):
        # Order matters: remove-then-write would lose the cooldown on a crash.
        src = (REPO / "src" / "check-pending-questions.py").read_text()
        body = src.split("def write_notify_stamp", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(body.index("write_text("), body.index('".last-pq-notify"'),
                        "the new stamp must be durable before the old one is retired")

    def test_a_fresh_install_with_no_root_stamp_is_unaffected(self):
        self.assertFalse(self.legacy.exists(), "precondition")
        cpq.write_notify_stamp([], now=1700000000)   # must not raise
        self.assertTrue(cpq.LAST_NOTIFY_FILE.is_file())

    def test_cleanup_targets_the_OVERRIDDEN_root_not_the_real_workspace(self):
        # Derived from LAST_NOTIFY_FILE, so a redirected stamp cannot reach the real
        # workspace root and delete an operator's file during a test run.
        real = cpq.WORKSPACE / ".last-pq-notify"
        existed = real.exists()
        cpq.write_notify_stamp([], now=1700000000)
        self.assertEqual(real.exists(), existed,
                         "a test must never touch the real workspace root")

    def test_an_unremovable_root_stamp_does_not_break_the_write(self):
        # A directory at that path makes unlink raise IsADirectoryError (an OSError).
        # The cooldown record matters more than the cleanup, so the write must stand.
        self.legacy.mkdir()
        cpq.write_notify_stamp([], now=1700000000)
        self.assertTrue(cpq.LAST_NOTIFY_FILE.is_file(),
                        "a failed cleanup must not cost the cooldown record")
        self.assertTrue(self.legacy.is_dir(), "left as found rather than forced")

    def _load_hc(self):
        hc_path = REPO / "src" / "health-check.py"
        if not hc_path.is_file():
            self.skipTest("health-check.py not present")
        import importlib.util as iu
        s = iu.spec_from_file_location("hc2", hc_path)
        hc = iu.module_from_spec(s); sys.modules["hc2"] = hc
        try:
            s.loader.exec_module(hc)
        except SystemExit:
            pass
        return hc

    def _root_tidy(self, hc):
        """Drives the SHIPPED probe: a local iterdir() copy would re-encode the
        property under test and would drop WORKSPACE_ROOT_SENTINEL_GLOBS."""
        orig = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws
        try:
            return hc.check_workspace_root_tidy()
        finally:
            hc.WORKSPACE_DIR = orig

    def test_root_tidy_is_clean_for_the_upgraded_workspace_afterwards(self):
        """The standing WARN must actually clear, not just stop being re-created."""
        hc = self._load_hc()
        self.legacy.write_text("1785876643 2f35a3c6")
        cpq.write_notify_stamp([], now=1700000000)
        self.assertIsNone(self._root_tidy(hc),
                          "root still not tidy after the upgrade")

    def test_the_probe_would_still_flag_the_old_root_stamp(self):
        """Negative control. Without it, a probe that returned None unconditionally —
        or a fixture that never wrote the file — would satisfy the assertion above."""
        hc = self._load_hc()
        cpq.write_notify_stamp([], now=1700000000)
        (self.ws / ".last-pq-notify").write_text("1785876643 2f35a3c6")
        result = self._root_tidy(hc)
        self.assertIsNotNone(result, "a root-level .last-pq-notify must still warn")
        self.assertEqual(result["status"], "warn")
        self.assertIn(".last-pq-notify", result["detail"])

    def test_a_migration_sentinel_at_the_root_is_not_flagged(self):
        """WORKSPACE_ROOT_SENTINEL_GLOBS exempts `.*-migrated*`, so a sentinel that
        production accepts must not be flagged."""
        hc = self._load_hc()
        cpq.write_notify_stamp([], now=1700000000)
        (self.ws / ".foo-migrated-123").write_text("x")
        self.assertIsNone(self._root_tidy(hc),
                          "a migration sentinel must not count as a loose file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
