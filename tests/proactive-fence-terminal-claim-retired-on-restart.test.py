#!/usr/bin/env python3
"""The production path: ProactiveClaimFence.confirm() crashing mid-complete.

`confirm()` unlinks the `.sending` body BEFORE `_finish()` runs `complete()`.
If the process dies after complete() writes the terminal status but before it
releases the claim, restart has neither the body nor the in-memory item id — so
nothing ever calls `claim()` for that item again. A cleanup hung off `claim()`
is therefore unreachable on the shipped path, and only a startup pass that
enumerates the claim records on disk can retire the record.

`DesignAClaimBackend.recover()` is that pass, and it could not do the job:
it globbed `*.json` while records are written `*.claim`, and it derived the
item id from `p.stem`, which is a lossy digest-suffixed safe key that cannot be
reversed. Both are exercised below.

The claiming pid must GENUINELY die — `_record_is_reclaimable` refuses to
displace an ALIVE owner, which is correct and would mask the fix.

Run: python3 tests/proactive-fence-terminal-claim-retired-on-restart.test.py
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
for _p in (str(_PKG), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import DesignAClaimBackend  # noqa: E402

# Real fence, real backend, real confirm(). Only the process death is injected,
# at the exact _release_locked boundary inside complete().
_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {pkg!r})
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from ag2_sparrow import outbox
    from ag2_sparrow.delivery_core import DesignAClaimBackend
    from proactive_claim_fence import ProactiveClaimFence

    results = Path({results!r})
    backend = DesignAClaimBackend(Path({root!r}), reclaim_ttl_s=0.0)
    fence = ProactiveClaimFence(backend, results)

    body = results / "proactive-1.txt"
    claim = fence.claim(body)
    assert claim is not None, "child could not claim the proactive body"

    def _die(*a, **k):
        os._exit(0)          # terminal status written, claim still held

    outbox._release_locked = _die
    fence.confirm(claim)     # unlinks the body, then completes -> dies
    os._exit(9)              # unreachable: confirm() must reach the release
    """
)


class TerminalClaimRetiredOnRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fence-restart-"))
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.root = self.tmp / "outbox"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _crash_a_confirm(self):
        (self.results / "proactive-1.txt").write_text("hello")
        src = _CHILD.format(pkg=str(_PKG), src=str(_REPO / "src"),
                            results=str(self.results), root=str(self.root))
        proc = subprocess.run([sys.executable, "-c", src],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"child did not die at the release boundary: "
                         f"rc={proc.returncode} err={proc.stderr[-500:]}")
        # Premises, asserted rather than assumed.
        claims = list((self.root / outbox.CLAIMS_DIR).glob("*.claim"))
        self.assertEqual(len(claims), 1, "the crash must leave exactly one claim")
        rec = outbox._read_claim_at(claims[0], "")
        self.assertFalse(_pid_alive(rec.pid),
                         "premise: the claiming pid must be GENUINELY dead")
        self.assertNotEqual(rec.item_id, claims[0].stem,
                            "premise: the filename is NOT the item id — this is "
                            "why deriving it from p.stem could never work")
        return rec

    def test_the_body_is_gone_so_nothing_can_ever_call_claim_again(self):
        """Why a claim()-time cleanup cannot reach this on the shipped path."""
        self._crash_a_confirm()
        self.assertFalse((self.results / "proactive-1.txt").exists(),
                         "confirm() unlinks the body before completing")
        self.assertEqual(list(self.results.glob("*")), [],
                         "no body and no .sending remain to re-derive an id from")

    def test_restart_recovery_retires_the_dead_terminal_claim(self):
        rec = self._crash_a_confirm()
        self.assertEqual(outbox._read_item(self.root, rec.item_id).get("status"),
                         "DELIVERED")

        report = DesignAClaimBackend(self.root, reclaim_ttl_s=0.0).recover()

        self.assertIn(rec.item_id, report.retired,
                      f"recovery must name what it retired: {report!r}")
        self.assertIsNone(outbox.read_delivery_claim(self.root, rec.item_id),
                          "the dead owner's claim must not survive recovery")
        self.assertEqual(outbox._read_item(self.root, rec.item_id).get("status"),
                         "DELIVERED", "retiring the claim must not resurrect it")

    def test_a_live_owners_claim_survives_recovery(self):
        """Mutation guard: recovery must never steal from a running drainer.

        Without this, 'retire the claim' could be an unconditional release and
        still pass the test above.
        """
        backend = DesignAClaimBackend(self.root, reclaim_ttl_s=0.0)
        backend.publish("live-item", b"x")
        self.assertIsNotNone(backend.claim("live-item", "me"))
        held = outbox.read_delivery_claim(self.root, "live-item")
        self.assertEqual(held.pid, os.getpid(), "premise: THIS process holds it")

        report = backend.recover()

        self.assertNotIn("live-item", report.retired)
        self.assertNotIn("live-item", report.recovered)
        self.assertIsNotNone(outbox.read_delivery_claim(self.root, "live-item"))

    def test_a_dead_owner_on_a_NON_terminal_item_is_reported_not_retired(self):
        """The pre-existing job of recover() still works — and now actually runs.

        Before the glob fix this returned [] for every input, so the two
        outcomes were indistinguishable from a scan that found nothing.
        """
        rec = self._crash_a_confirm()
        d = outbox._read_item(self.root, rec.item_id)
        d["status"] = "READY"
        outbox._write_item(self.root, rec.item_id, d)

        report = DesignAClaimBackend(self.root, reclaim_ttl_s=0.0).recover()

        self.assertIn(rec.item_id, report.recovered)
        self.assertNotIn(rec.item_id, report.retired)


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
