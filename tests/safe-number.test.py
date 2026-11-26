#!/usr/bin/env python3
"""safe_number.finite: a number from untrusted JSON as a finite float, or None; never raises.

Run: python3 tests/safe-number.test.py
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import safe_number  # noqa: E402


class Finite(unittest.TestCase):
    def test_only_a_finite_int_or_float_is_a_number(self):
        for v, want in ((5, 5.0), (2.5, 2.5), (-1, -1.0), (0, 0.0)):
            self.assertEqual(safe_number.finite(v), want)
        for v in (10 ** 400, -10 ** 400, float("nan"), float("inf"), float("-inf"), True, False, "1", None, [1]):
            with self.subTest(v=repr(v)[:20]):
                self.assertIsNone(safe_number.finite(v))
        with self.assertRaises(OverflowError):
            math.isfinite(10 ** 400)     # why the helper exists


if __name__ == "__main__":
    unittest.main()
