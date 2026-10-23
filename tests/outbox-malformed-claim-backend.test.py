#!/usr/bin/env python3
"""A malformed claim through DesignAClaimBackend: never taken on the torn-write
grace, and still retired from a terminal item once its owner is gone past the TTL.

Run: python3 tests/outbox-malformed-claim-backend.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import DesignAClaimBackend  # noqa: E402

_dead = subprocess.Popen([sys.executable, "-c", "pass"])
_dead.wait()
DEAD_PID = _dead.pid
ITEM = "room-evt-malformed"
TTL = 300.0


class MalformedClaimBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rewrite(self, backend, patch=None, text=None, age=0.0):
        p = outbox._claim_path(self.root, ITEM)
        if text is None:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec.update(patch)
            text = json.dumps(rec)
        p.write_text(text, encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))

    def test_live_owner_bad_token_not_taken_after_grace(self):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=1e9)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        self._rewrite(b, {"start_usec": -1}, age=outbox.SWAP_GRACE_S + 1)
        self.assertIsNone(b.claim(ITEM, "B"), "a live owner's malformed claim was taken")
        self.assertEqual(outbox.read_delivery_claim(self.root, ITEM).drainer_id, "A")

    def test_torn_claim_still_taken_after_grace(self):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=1e9)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        self._rewrite(b, text='{"item_id": "room-evt-mal', age=outbox.SWAP_GRACE_S + 1)
        self.assertIsNotNone(b.claim(ITEM, "B"), "a torn claim must still age out")

    def _terminal_malformed(self, age):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        outbox.record_delivered(self.root, ITEM)
        self._rewrite(b, {"pid": DEAD_PID, "start_usec": -1}, age=age)
        return b

    def test_terminal_malformed_claim_retired_at_ttl(self):
        b = self._terminal_malformed(TTL)
        self.assertEqual(b.recover().retired, [ITEM])
        self.assertIsNone(outbox.read_delivery_claim(self.root, ITEM))
        self.assertTrue(b.publish(ITEM, b"again"))

    def test_terminal_malformed_claim_kept_before_ttl(self):
        b = self._terminal_malformed(TTL - 1)
        self.assertEqual(b.recover().retired, [])
        self.assertIsNotNone(outbox.read_delivery_claim(self.root, ITEM))


if __name__ == "__main__":
    unittest.main()
