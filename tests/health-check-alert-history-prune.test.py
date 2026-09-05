#!/usr/bin/env python3
"""A malformed alert-history value must not kill the health-check run.

Measured 2026-09-01 on a live host: one entry of the wrong type raised inside
the prune comprehension and took `main()` down through `notify_for_failures`,
so every step after it — `--recover-core` included — never ran.

Run: python3 tests/health-check-alert-history-prune.test.py
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

NOW = 1_788_000_000_000
CUTOFF = NOW - (24 * 3600 * 1000)


class PruneAlertHistoryTest(unittest.TestCase):
    def test_a_malformed_value_is_dropped_not_raised(self) -> None:
        """The live crash: a dict where a timestamp belongs."""
        out = hc._prune_alert_history({"good": NOW, "bad": {"nested": 1}}, CUTOFF)
        self.assertEqual(out, {"good": NOW})

    def test_every_non_numeric_shape_is_survivable(self) -> None:
        for bad in ({"a": 1}, ["x"], "str", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    hc._prune_alert_history({"k": bad, "ok": NOW}, CUTOFF), {"ok": NOW})

    def test_the_last_hash_key_survives_being_a_string(self) -> None:
        """It is a hash, not a timestamp — excluded from the age compare."""
        out = hc._prune_alert_history({hc._LAST_HASH_KEY: "abc123", "old": 1}, CUTOFF)
        self.assertEqual(out, {hc._LAST_HASH_KEY: "abc123"})

    def test_pruning_still_prunes(self) -> None:
        """The guard must not turn this into keep-everything."""
        out = hc._prune_alert_history({"fresh": NOW, "stale": CUTOFF - 1}, CUTOFF)
        self.assertEqual(out, {"fresh": NOW})

    def test_the_cutoff_boundary_is_inclusive(self) -> None:
        self.assertEqual(hc._prune_alert_history({"edge": CUTOFF}, CUTOFF), {"edge": CUTOFF})

    def test_a_bool_is_not_a_timestamp(self) -> None:
        """bool subclasses int, so `isinstance(v, int)` alone would keep True."""
        self.assertEqual(hc._prune_alert_history({"b": True, "ok": NOW}, CUTOFF), {"ok": NOW})

    def test_every_call_site_delegates(self) -> None:
        """Four copies of this policy existed; none may come back."""
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertNotIn("or v >= cutoff", src,
                         "an inline prune copy is back — delegate to _prune_alert_history")
        self.assertEqual(src.count("_prune_alert_history(history, cutoff)"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
