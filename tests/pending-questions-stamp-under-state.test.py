#!/usr/bin/env python3
"""The notify-cooldown stamp belongs under state/, not at the workspace root.

At the root it was real drift: the contract reserves the root for top-level
directories plus the artifacts WORKSPACE_SURFACE_FILES names, and this stamp is
neither — so health-check's workspace-root-tidy probe flagged it on every run.
A permanent WARN is how a correct detector gets ignored.

No read-fallback to the old path on purpose. `_last_notified` already treats a
missing stamp as "set unknown" and notifies ONCE rather than suppressing, so the
transition costs one notification; a second path would also leak the real root
file into the tests that override this constant with a tmpdir.
"""
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
        """The probe only inspects files directly AT the root, so state/ is exempt.

        Asserted through health-check's own predicate rather than by reasoning, so
        this fails if that contract ever changes.
        """
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
        self.assertEqual(key, cpq.questions_key([]), "the key must be the reader's key")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
