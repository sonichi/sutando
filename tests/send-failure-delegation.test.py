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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ
# Before anything below reads the environment: the park probe imports the gateway bridge.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import send_failure_policy as sfp  # noqa: E402
from proactive_recovery import release_claim  # noqa: E402

BRIDGE = (REPO / "src" / "discord-bridge.py").read_text()


class TestDelegation(unittest.TestCase):
    def test_bridge_imports_the_policy(self):
        self.assertIn("import send_failure_policy", BRIDGE)

    def test_both_quarantine_sites_consult_the_policy(self):
        # The approval site keeps its own file (the marker IS the obligation) but must
        # still take the CAP from the policy, or it is an unbounded 3s hot loop.
        # The proactive site consults it through the fence (5b), whose own
        # consult is pinned below and behaviorally by its unit suite.
        self.assertIn("_proactive_fence().fail", BRIDGE)
        self.assertIn("send_failure_policy.should_retry", BRIDGE)
        fence = (REPO / "src" / "proactive_claim_fence.py").read_text()
        self.assertIn("decide_failed_send(", fence)

    def test_no_site_retries_without_a_cap(self):
        # `is_transient` alone answers "could a retry work", never "how many times".
        # Using it as a retry gate is what made the approval branch unbounded.
        for line in BRIDGE.splitlines():
            t = line.strip()
            if t.startswith("#") or "is_transient" not in t:
                continue
            self.assertNotRegex(t, r"^if .*is_transient\(",
                                f"uncapped retry gate: {t}")

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
                         "the file move belongs to the fence executor")
        self.assertIn("_proactive_fence().fail", window)
        self.assertIn("continue", window)

    def test_the_policy_module_owns_the_move(self):
        policy = (REPO / "src" / "send_failure_policy.py").read_text()
        self.assertIn('/ "undelivered"', policy)
        self.assertIn("release_claim", policy)


SPARROW = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
           / "remote_gateway_bridge.py").read_text()


class TestSparrowDelegation(unittest.TestCase):
    """The gateway bridge's proactive send keeps no retry of its own: the outbox decides."""

    def test_sparrow_delegates_the_transition(self):
        self.assertNotIn("def _resolve_send_failure", SPARROW)
        body = SPARROW.split("def _send_proactive", 1)[1]
        body = body.split("\ndef ", 1)[0]
        # The outbox owns the claim, the retry and the outcome; the drain moves the file
        # as the outcome says and keeps no attempt count, which is how a third copy starts.
        self.assertIn("core.deliver_one(", body)
        self.assertNotIn("MAX_TRANSIENT_ATTEMPTS", body)
        self.assertNotIn("resolve_failed_send", body)
        self.assertNotIn("should_retry", body)

    def test_sparrow_parks_to_the_repo_wide_quarantine(self):
        # results/undelivered/ is what health-check's probe scans; an archive/ subdir has no reader.
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "-c", _PARK_PROBE], capture_output=True, text=True, timeout=120,
                env=hermetic_env(tmp, PYTHONPATH=str(REPO / "packages" / "ag2-sparrow"),
                                 AGENT_CONNECT_RESULT_DIR=str(Path(tmp) / "results"),
                                 AGENT_CONNECT_TASK_DIR=str(Path(tmp) / "tasks"),
                                 AGENT_CONNECT_STATE_DIR=str(Path(tmp) / "state"),
                                 REMOTE_PROACTIVE_ROOM="", AGENT_MXID="@a:ag2.space"))
            self.assertEqual(out.returncode, 0, out.stderr)
            results = Path(tmp) / "results"
            self.assertEqual(out.stdout.strip(), "parked")
            (parked,) = (results / "undelivered").glob("proactive-park-*.txt")
            self.assertIn("body", parked.read_text())
            self.assertFalse(list(results.glob("proactive-park*.txt")), "nothing left queued")


# A proactive file whose send times out, through the bridge's real drain: the outbox parks it.
_PARK_PROBE = """
import time
from ag2_sparrow import remote_gateway_bridge as gb
gb.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
(gb.RESULTS_DIR / "proactive-park.txt").write_text("[channel: !room:ag2.space]\\nbody\\n")
def slow(*a, **k):
    raise TimeoutError("no answer")
gb._req, gb._log, gb.PROACTIVE_ROOM, gb.PROACTIVE_CLAIM_GATE = slow, lambda *a: None, "", None
gb._ROUTING.update(owner_dm="", loaded=True, next=time.time() + 3600)
gb._post_proactive()
print("parked" if list((gb.RESULTS_DIR / "undelivered").glob("proactive-park-*.txt")) else "not parked")
"""


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

    def test_release_honors_an_explicit_target_for_pid_scoped_claims(self):
        d = Path(tempfile.mkdtemp())
        claim = d / "proactive-x.sending.4321"
        claim.write_text("body")
        self.assertTrue(release_claim(claim, target=d / "proactive-x.txt"))
        self.assertTrue((d / "proactive-x.txt").exists(),
                        "explicit target restores the real polled name")

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
