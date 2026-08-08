#!/usr/bin/env python3
"""Guards the discord-bridge → send_failure_policy boundary.

Three things must stay true:
  1. Both quarantine sites consult the policy rather than parking every failure.
     Before the extraction each `except` moved the file aside unconditionally, so a
     503 — which clears on the next 3s poll — stranded an owner-facing body.
  2. Neither site rebuilds its own status classification. A local `status == 503`
     is how the two copies drift apart again.
  3. The retry path releases the claim (`.sending` → `.txt`), because that, and not
     the log line, is what actually returns the body to the polling stream.

The source scan is TOKEN-SPECIFIC — it flags inline status comparisons, not the
words "transient" or "retry" — so renaming cannot satisfy it.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import send_failure_policy as sfp  # noqa: E402
from proactive_recovery import release_claim  # noqa: E402

BRIDGE = (REPO / "src" / "discord-bridge.py").read_text()


class TestDelegation(unittest.TestCase):
    def test_bridge_imports_the_policy(self):
        self.assertIn("import send_failure_policy", BRIDGE)

    def test_both_quarantine_sites_consult_the_policy(self):
        # The proactive DM site and the approval-marker site carry the same rule.
        self.assertIn("send_failure_policy.resolve_failed_send", BRIDGE)
        self.assertIn("send_failure_policy.is_transient", BRIDGE)

    def test_no_site_reimplements_status_classification(self):
        # An inline comparison against a transient status is the drift signature.
        offenders = []
        for m in re.finditer(r"^(?!\s*#).*\.status\s*(==|>=|in)\s*[^\n]*", BRIDGE, re.M):
            line = m.group(0)
            if any(str(s) in line for s in sfp.TRANSIENT_STATUSES):
                offenders.append(line.strip())
        self.assertEqual(offenders, [], f"inline status classification: {offenders}")

    def test_the_bridge_does_not_move_the_file_itself(self):
        # The whole point of the extraction: the bridge must not carry its own
        # rename-to-undelivered, which is how the two copies drifted apart.
        window = BRIDGE.split("failed to DM", 1)[1][:1400]
        self.assertNotIn('/ "undelivered"', window,
                         "the file move belongs to resolve_failed_send")
        self.assertIn("resolve_failed_send", window)
        self.assertIn("continue", window)

    def test_the_policy_module_owns_the_move(self):
        policy = (REPO / "src" / "send_failure_policy.py").read_text()
        self.assertIn('/ "undelivered"', policy)
        self.assertIn("release_claim", policy)


class TestReleaseClaimActuallyReturnsTheBody(unittest.TestCase):
    """The retry depends on release_claim restoring the polled name."""

    def test_sending_becomes_txt_and_is_visible_to_a_txt_poll(self):
        d = Path(tempfile.mkdtemp())
        claim = d / "proactive-x.sending"
        claim.write_text("body")
        self.assertTrue(release_claim(claim))
        polled = [p.name for p in d.iterdir() if p.name.startswith("proactive-")
                  and p.suffix == ".txt"]
        self.assertEqual(polled, ["proactive-x.txt"],
                         "after a transient failure the body must be re-pollable")

    def test_release_refuses_to_clobber_an_existing_txt(self):
        d = Path(tempfile.mkdtemp())
        (d / "proactive-x.txt").write_text("newer body")
        claim = d / "proactive-x.sending"
        claim.write_text("older body")
        self.assertFalse(release_claim(claim),
                         "must not overwrite a body written since the claim")
        self.assertEqual((d / "proactive-x.txt").read_text(), "newer body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
