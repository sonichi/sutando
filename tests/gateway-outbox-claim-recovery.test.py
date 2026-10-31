#!/usr/bin/env python3
"""The gateway's sweep retires the claim a crash left on a terminal outbox item.

DesignAClaimBackend.complete() writes the terminal status, then releases the
claim. A process that dies between the two leaves a claim that no later
claim() reaches: the gateway archives the result, so nothing re-derives that
item id. Only DeliveryCore.recover() does, and the gateway never called it.

recover() is safe to run on a cadence because it releases a claim only on a
TERMINAL item whose owner is DEAD and past the reclaim TTL. The owner must
genuinely die here (a child process), or the liveness check would refuse, as
it must, and mask the fix.

Run: python3 tests/gateway-outbox-claim-recovery.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import outbox, remote_gateway_bridge as gw  # noqa: E402
from ag2_sparrow.delivery_core import DesignAClaimBackend  # noqa: E402

_CORES = ("_DELIVERY_CORE", "_RELAY_CORE", "_WITHHELD_OUTBOX", "_PROACTIVE_CORE")

# Claim, write the terminal status, and die before the release: complete()'s crash window.
_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {pkg!r})
    from pathlib import Path
    from ag2_sparrow import outbox
    from ag2_sparrow.delivery_core import DesignAClaimBackend
    root, item, terminal = Path({root!r}), {item!r}, {terminal!r}
    backend = DesignAClaimBackend(root)
    backend.publish(item, b"the reply")
    assert backend.claim(item, "gateway-result-drain") is not None
    if terminal:
        with outbox._item_lock(root, item):
            outbox.record_delivered(root, item, provider="AG2SpaceResultProvider")
    os._exit(0)
    """
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class OutboxClaimRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="outbox-recover-")
        self._saved = {n: getattr(gw, n) for n in (
            "RESULTS_DIR", "_log", "_last_orphan_sweep", "_delivery_core") + _CORES}
        gw.RESULTS_DIR = Path(self.tmp.name) / "results"
        gw.RESULTS_DIR.mkdir(parents=True)
        for n in _CORES:
            setattr(gw, n, None)
        self.logs: list = []
        gw._log = lambda m: self.logs.append(str(m))

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        self.tmp.cleanup()

    def root(self, lane="result") -> Path:
        return gw._delivery_core(lane).backend.root

    def crash(self, item, lane="result", terminal=True, age_s=3600.0):
        """A dead owner's claim on `item`, its record aged `age_s` seconds."""
        root = self.root(lane)
        proc = subprocess.run([sys.executable, "-c", _CHILD.format(
            pkg=str(_PKG), root=str(root), item=item, terminal=terminal)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        rec = outbox.read_delivery_claim(root, item)
        self.assertIsNotNone(rec, "premise: the crash leaves the claim")
        self.assertFalse(_pid_alive(rec.pid), "premise: the claiming pid is genuinely dead")
        self.age(root, item, age_s)
        return root

    def age(self, root, item, age_s):
        for p in outbox._claims_dir(root).glob("*.claim"):
            d = json.loads(p.read_text())
            if d.get("item_id") == item:
                d["claimed_at"] = time.time() - age_s
                p.write_text(json.dumps(d))

    def sweep(self):
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())

    def held(self, root, item):
        return outbox.read_delivery_claim(root, item) is not None

    def test_the_sweep_retires_a_dead_owners_claim_on_a_delivered_item(self):
        root = self.crash("task-dead-terminal")
        self.sweep()
        self.assertFalse(self.held(root, "task-dead-terminal"))
        self.assertEqual(outbox._read_item(root, "task-dead-terminal")["status"], "DELIVERED",
                         "retiring the claim must not change the item")
        self.assertTrue(any("task-dead-terminal" in m and "retired 1 claim" in m for m in self.logs),
                        self.logs)

    def test_every_lane_root_is_swept(self):
        roots = {lane: self.crash(f"task-{lane}", lane=lane) for lane in ("withheld", "proactive")}
        self.sweep()
        for lane, root in roots.items():
            with self.subTest(lane=lane):
                self.assertFalse(self.held(root, f"task-{lane}"))

    def test_a_claim_inside_the_reclaim_ttl_is_left_alone(self):
        root = self.crash("task-fresh", age_s=0.0)
        self.sweep()
        self.assertTrue(self.held(root, "task-fresh"), "the sweep must honour the reclaim TTL")

    def test_a_live_owners_claim_on_a_terminal_item_is_never_taken(self):
        root = self.root()
        backend = DesignAClaimBackend(root)
        backend.publish("task-live", b"x")
        self.assertIsNotNone(backend.claim("task-live", "me"))
        with outbox._item_lock(root, "task-live"):
            outbox.record_delivered(root, "task-live", provider="AG2SpaceResultProvider")
        self.age(root, "task-live", 3600.0)
        self.sweep()
        self.assertTrue(self.held(root, "task-live"), "a running drainer is never displaced")

    def test_a_dead_owners_claim_on_an_undelivered_item_is_not_released_by_the_sweep(self):
        # Only a terminal item's claim is retired; a live item is claim()'s to re-drive.
        root = self.crash("task-dead-queued", terminal=False)
        self.sweep()
        self.assertTrue(self.held(root, "task-dead-queued"))
        self.assertNotIn("DELIVERED", outbox._read_item(root, "task-dead-queued").get("status", ""))

    def test_one_lane_failing_does_not_stop_the_others_and_is_reported(self):
        root = self.crash("task-dead-terminal")
        real = gw._delivery_core

        def flaky(lane="result"):
            if lane == "withheld":
                raise OSError("withheld outbox unreadable")
            return real(lane)
        gw._delivery_core = flaky
        self.sweep()
        self.assertFalse(self.held(root, "task-dead-terminal"))
        self.assertTrue(any("withheld" in m and "claim recovery failed" in m for m in self.logs),
                        self.logs)


if __name__ == "__main__":
    unittest.main(verbosity=1)
