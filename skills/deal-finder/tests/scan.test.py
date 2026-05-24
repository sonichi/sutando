#!/usr/bin/env python3
"""
Tests for skills/deal-finder/scripts/scan.py

Coverage:
  parse_price     — dollar sign, commas, empty string, None-worthy input
  extract_chip    — M1, M2, M3, pro/max suffixes, multi-chip (highest wins),
                    model-number false positive rejected, Intel chip not matched,
                    no chip
  extract_ram_gb  — requires RAM context, "256GB SSD" not RAM, multi-mention,
                    values <4 rejected, values >256 rejected
  extract_storage_gb — GB SSD, TB → GB×1024, TB hint dominates, no mention
  passes          — no mac mini title, accessory listing, Intel chip, excluded chip,
                    chip not in wanted, RAM too low, storage too low, price too high,
                    soft match when specs missing, full match

Run: python3 skills/deal-finder/tests/scan.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

# scan.py uses `int | None` union syntax (PEP 604) which requires Python 3.10+.
if sys.version_info < (3, 10):
    print(f"SKIP: scan.py requires Python 3.10+ (got {sys.version.split()[0]}). Run with python3.10.")
    sys.exit(0)

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "scan",
    REPO / "skills" / "deal-finder" / "scripts" / "scan.py",
)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


_BASE_CRITERIA = {
    "search_query": "mac mini",
    "chips": ["m2", "m3"],
    "exclude_chips": [],
    "min_ram_gb": 16,
    "min_storage_gb": 256,
    "max_price_usd": 800,
    "zip": "94102",
    "search_radius_miles": 50,
    "soft_match_when_specs_missing": True,
}


def _listing(title="Mac Mini M2 16GB RAM 512GB SSD", body="", price_int=700):
    return {"title": title, "body": body, "price_int": price_int}


# ── parse_price ───────────────────────────────────────────────────────────────

class TestParsePrice(unittest.TestCase):
    def test_dollar_sign_stripped(self):
        self.assertEqual(sc.parse_price("$500"), 500)

    def test_commas_stripped(self):
        self.assertEqual(sc.parse_price("$1,200"), 1200)

    def test_empty_returns_none(self):
        self.assertIsNone(sc.parse_price(""))

    def test_no_digits_returns_none(self):
        self.assertIsNone(sc.parse_price("$"))

    def test_plain_number(self):
        self.assertEqual(sc.parse_price("750"), 750)


# ── extract_chip ──────────────────────────────────────────────────────────────

class TestExtractChip(unittest.TestCase):
    def test_m2_detected(self):
        self.assertEqual(sc.extract_chip("Mac Mini M2 16GB RAM"), "M2")

    def test_m3_detected(self):
        self.assertEqual(sc.extract_chip("Mac mini M3 pro chip 36GB"), "M3")

    def test_highest_chip_wins(self):
        # "M2 and M4" → highest is M4
        self.assertEqual(sc.extract_chip("M2 or M4 Mac mini"), "M4")

    def test_m1_detected(self):
        self.assertEqual(sc.extract_chip("Apple M1 Mac mini"), "M1")

    def test_pro_suffix_ok(self):
        self.assertEqual(sc.extract_chip("M2 Pro Mac mini"), "M2")

    def test_max_suffix_ok(self):
        self.assertEqual(sc.extract_chip("M4 Max Mac mini"), "M4")

    def test_model_number_not_matched(self):
        # "M475" or "M9340" should NOT be treated as chip
        self.assertIsNone(sc.extract_chip("GPU model M475 Mac mini"))

    def test_no_chip_returns_none(self):
        self.assertIsNone(sc.extract_chip("Mac mini Intel i7 2018"))

    def test_no_mention_returns_none(self):
        self.assertIsNone(sc.extract_chip("Apple Mac mini for sale"))


# ── extract_ram_gb ────────────────────────────────────────────────────────────

class TestExtractRamGb(unittest.TestCase):
    def test_explicit_ram_context(self):
        self.assertEqual(sc.extract_ram_gb("16GB RAM"), 16)

    def test_memory_context(self):
        self.assertEqual(sc.extract_ram_gb("24GB memory"), 24)

    def test_ssd_not_ram(self):
        # "256GB SSD" should not match as RAM (Chi #648 fix)
        self.assertIsNone(sc.extract_ram_gb("256GB SSD storage"))

    def test_unified_memory_context(self):
        self.assertEqual(sc.extract_ram_gb("36GB unified memory"), 36)

    def test_multi_mention_returns_max(self):
        # "8GB RAM ... 16GB RAM" → max = 16
        self.assertEqual(sc.extract_ram_gb("8GB RAM or upgrade to 16GB RAM"), 16)

    def test_value_below_4_rejected(self):
        self.assertIsNone(sc.extract_ram_gb("2GB RAM"))

    def test_value_above_256_rejected(self):
        self.assertIsNone(sc.extract_ram_gb("512GB RAM"))

    def test_no_mention_returns_none(self):
        self.assertIsNone(sc.extract_ram_gb("Mac mini M2 512GB SSD"))


# ── extract_storage_gb ────────────────────────────────────────────────────────

class TestExtractStorageGb(unittest.TestCase):
    def test_gb_ssd(self):
        self.assertEqual(sc.extract_storage_gb("512GB SSD"), 512)

    def test_tb_converts_to_gb(self):
        self.assertEqual(sc.extract_storage_gb("1TB SSD"), 1024)

    def test_tb_hint_dominates(self):
        # If "1TB" appears anywhere, that wins
        result = sc.extract_storage_gb("2TB NVMe storage")
        self.assertEqual(result, 2048)

    def test_no_mention_returns_none(self):
        self.assertIsNone(sc.extract_storage_gb("Mac mini M2 16GB RAM"))

    def test_fractional_tb(self):
        result = sc.extract_storage_gb("0.5TB storage")
        self.assertEqual(result, 512)


# ── passes ────────────────────────────────────────────────────────────────────

class TestPasses(unittest.TestCase):
    def test_no_mac_mini_in_title(self):
        ok, reason = sc.passes(_BASE_CRITERIA, _listing(title="Apple M2 computer"))
        self.assertFalse(ok)
        self.assertIn("mac mini", reason)

    def test_accessory_listing_rejected(self):
        ok, reason = sc.passes(_BASE_CRITERIA, _listing(title="Mac mini power cord adapter"))
        self.assertFalse(ok)
        self.assertIn("accessory", reason)

    def test_intel_chip_rejected(self):
        ok, reason = sc.passes(_BASE_CRITERIA, _listing(title="Mac Mini i7-8700 2018"))
        self.assertFalse(ok)
        self.assertIn("Intel", reason)

    def test_no_chip_detected_rejected(self):
        ok, reason = sc.passes(_BASE_CRITERIA, _listing(title="Mac Mini 2021 great condition"))
        self.assertFalse(ok)
        self.assertIn("chip", reason)

    def test_excluded_chip_rejected(self):
        criteria = {**_BASE_CRITERIA, "exclude_chips": ["m2"]}
        ok, reason = sc.passes(criteria, _listing(title="Mac Mini M2 16GB 512GB SSD"))
        self.assertFalse(ok)
        self.assertIn("excluded", reason)

    def test_chip_not_in_wanted_rejected(self):
        criteria = {**_BASE_CRITERIA, "chips": ["m3"]}
        ok, reason = sc.passes(criteria, _listing(title="Mac Mini M2 16GB RAM 512GB SSD"))
        self.assertFalse(ok)
        self.assertIn("not in", reason)

    def test_ram_too_low_rejected(self):
        ok, reason = sc.passes(
            {**_BASE_CRITERIA, "min_ram_gb": 24, "soft_match_when_specs_missing": False},
            _listing(title="Mac Mini M2 8GB RAM 512GB SSD"),
        )
        self.assertFalse(ok)
        self.assertIn("RAM", reason)

    def test_storage_too_low_rejected(self):
        ok, reason = sc.passes(
            {**_BASE_CRITERIA, "min_storage_gb": 512, "soft_match_when_specs_missing": False},
            _listing(title="Mac Mini M2 16GB RAM 256GB SSD"),
        )
        self.assertFalse(ok)
        self.assertIn("storage", reason)

    def test_price_too_high_rejected(self):
        ok, reason = sc.passes(
            _BASE_CRITERIA,
            _listing(title="Mac Mini M2 16GB RAM 512GB SSD", price_int=900),
        )
        self.assertFalse(ok)
        self.assertIn("price", reason)

    def test_full_match(self):
        ok, reason = sc.passes(
            _BASE_CRITERIA,
            _listing(title="Mac Mini M2 16GB RAM 512GB SSD", price_int=700),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "match")

    def test_soft_match_when_specs_missing(self):
        ok, reason = sc.passes(
            _BASE_CRITERIA,
            _listing(title="Mac Mini M2 for sale", price_int=700),
        )
        self.assertTrue(ok)
        self.assertIn("soft", reason)

    def test_no_price_int_not_rejected(self):
        ok, reason = sc.passes(
            _BASE_CRITERIA,
            _listing(title="Mac Mini M2 16GB RAM 512GB SSD", price_int=None),
        )
        self.assertTrue(ok)

    def test_empty_chips_list_allows_any_chip(self):
        criteria = {**_BASE_CRITERIA, "chips": []}
        ok, reason = sc.passes(criteria, _listing(title="Mac Mini M4 16GB RAM 512GB SSD"))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
