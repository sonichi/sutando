#!/usr/bin/env python3
"""A crash between complete()'s terminal write and its claim release.

`DesignAClaimBackend.complete()` writes `status = "DELIVERED"` and only then
calls `_release_locked`. A process that dies in that window leaves a terminal
item carrying a live claim record owned by a now-dead pid.

`outbox.reclaim_delivery_claim` already handles exactly that shape — the gap
was that `claim()` returns on terminal status BEFORE the reclaim path is ever
consulted, so nothing reached it and the record leaked permanently. The item is
DELIVERED, so this is not delivery loss; what leaks is a claim record that makes
"claims present" an unreliable signal.

The claiming pid must be GENUINELY DEAD or these assertions pass for the wrong
reason: `_record_is_reclaimable` refuses to displace an ALIVE owner, which is
correct behavior and would mask the fix. So the crash runs in a real child
process, and the child runs the REAL `complete()` — only the process death is
injected, at exactly the `_release_locked` boundary.

Run: python3 tests/delivery-core-crash-between-terminal-and-release.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import (  # noqa: E402
    DesignAClaimBackend, DeliveryOutcome)

ITEM = "room-evt-crash"

# Runs the production claim + complete, then dies AT the release call. Patching
# the boundary (not reimplementing complete()) keeps the real writer on the path.
_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {pkg!r})
    from ag2_sparrow import outbox
    from ag2_sparrow.delivery_core import DesignAClaimBackend, DeliveryOutcome

    b = DesignAClaimBackend({root!r}, reclaim_ttl_s=0.0)
    tok = b.claim({item!r}, "child-drainer")
    assert tok is not None, "child could not claim"

    def _die(*a, **k):
        os._exit(0)                      # terminal status written, claim intact

    outbox._release_locked = _die
    b.complete(tok, DeliveryOutcome.CONFIRMED)
    os._exit(9)                          # unreachable: complete() must reach release
    """
)


class CrashBetweenTerminalWriteAndReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dc-crash-"))
        self.backend = DesignAClaimBackend(self.tmp, reclaim_ttl_s=0.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _crash_a_delivery(self) -> int:
        """Publish, then let a real child die mid-complete. Returns its pid."""
        self.assertTrue(self.backend.publish(ITEM, b"payload"))
        src = _CHILD.format(pkg=str(_PKG), root=str(self.tmp), item=ITEM)
        proc = subprocess.run([sys.executable, "-c", src],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"child did not die at the release boundary: "
                         f"rc={proc.returncode} err={proc.stderr[-400:]}")
        # Premise, asserted rather than assumed: the window really is open.
        self.assertEqual(outbox._read_item(self.tmp, ITEM).get("status"), "DELIVERED")
        rec = outbox.read_delivery_claim(self.tmp, ITEM)
        self.assertIsNotNone(rec, "premise: the crash must leave a claim behind")
        self.assertFalse(_pid_alive(rec.pid),
                         "premise: the claiming pid must be GENUINELY dead, or "
                         "_record_is_reclaimable refuses for the right reason "
                         "and the test passes for the wrong one")
        return rec.pid

    def test_the_leaked_claim_is_cleared_on_the_next_claim_attempt(self):
        self._crash_a_delivery()

        token = self.backend.claim(ITEM, "next-drainer")

        self.assertIsNone(token, "a DELIVERED item must never be re-claimed")
        self.assertIsNone(outbox.read_delivery_claim(self.tmp, ITEM),
                          "the dead owner's claim must not survive the attempt")

    def test_the_item_stays_delivered(self):
        """Clearing the claim must not resurrect or re-deliver the item."""
        self._crash_a_delivery()
        self.backend.claim(ITEM, "next-drainer")
        self.assertEqual(outbox._read_item(self.tmp, ITEM).get("status"), "DELIVERED")

    def test_a_live_owners_claim_on_a_terminal_item_is_left_alone(self):
        """Mutation guard: never steal from a running drainer.

        Without this, 'clear the claim' could be implemented as an
        unconditional release and still pass the test above.
        """
        self.assertTrue(self.backend.publish(ITEM, b"payload"))
        token = self.backend.claim(ITEM, "live-drainer")
        self.assertIsNotNone(token)
        d = outbox._read_item(self.tmp, ITEM)
        d["status"] = "DELIVERED"
        outbox._write_item(self.tmp, ITEM, d)
        rec = outbox.read_delivery_claim(self.tmp, ITEM)
        self.assertEqual(rec.pid, os.getpid(), "premise: THIS live process owns it")

        self.assertIsNone(self.backend.claim(ITEM, "other-drainer"))

        self.assertIsNotNone(outbox.read_delivery_claim(self.tmp, ITEM),
                             "a live owner's claim must survive")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    unittest.main(verbosity=2)
