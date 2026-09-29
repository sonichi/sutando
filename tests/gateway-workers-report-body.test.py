#!/usr/bin/env python3
"""The bridge POSTs the per-worker report when the advertisement carries one:
only it tells the broker a worker is retired. Without it, the legacy body."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")

from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402

LEGACY = {"ts": 1, "live_cores": ["a" * 32], "dead_cores": []}
REPORT = {"ts": 1, "roster_version": 3,
          "workers": [{"id": "a" * 32, "state": "live"}, {"id": "b" * 32, "state": "retired"}],
          "applied": {"labels": {}, "bindings": {}}}


class WorkersBody(unittest.TestCase):
    def test_a_report_is_posted_so_retired_workers_are_known(self):
        self.assertIs(rgb._workers_body({"workers": LEGACY, "report": REPORT}), REPORT)

    def test_no_report_keeps_the_legacy_body(self):
        self.assertIs(rgb._workers_body({"workers": LEGACY}), LEGACY)

    def test_a_malformed_report_keeps_the_legacy_body(self):
        for bad in ("x", {"workers": "nope"}, {}):
            with self.subTest(bad=bad):
                self.assertIs(rgb._workers_body({"workers": LEGACY, "report": bad}), LEGACY)


if __name__ == "__main__":
    unittest.main()
