#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import phone_owner


class PhoneOwnerTests(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(phone_owner.normalize_phone("(415) 555-1234"), "14155551234")
        self.assertEqual(phone_owner.normalize_phone("+1 415-555-1234"), "14155551234")
        self.assertEqual(phone_owner.normalize_phone(""), "")

    def test_sender_tier(self):
        self.assertEqual(phone_owner.phone_sender_tier("(415) 555-1234", "+14155551234"), "owner")
        self.assertEqual(phone_owner.phone_sender_tier("+14155551234", "+14155550000, (415) 555-1234"), "owner")
        self.assertEqual(phone_owner.phone_sender_tier("+14155550000", "+14155551234"), "other")
        self.assertEqual(phone_owner.phone_sender_tier("+14155550000", ""), "other")
        self.assertEqual(phone_owner.phone_sender_tier("", "+14155551234"), "other")


if __name__ == "__main__":
    unittest.main(verbosity=1)
