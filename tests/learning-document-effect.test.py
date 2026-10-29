import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/learning-window/scripts"))
from document_effect import compare_inventory, digest


def observation(**changes):
    return {"readback": "verified", "store_identity": "person-1", "person_key": "alice",
            "observed_at": "2026-10-05T09:00:00+00:00", "document_sha256": digest("original"), **changes}


class RetentionTests(unittest.TestCase):
    def compare(self, last):
        return compare_inventory({"alice": observation()}, {"alice": last})["alice"]

    def test_same_hash_preserves_unknown_semantics(self):
        result = self.compare(observation(observed_at="2026-10-05T09:05:00+00:00"))
        self.assertEqual(result["physical_retention"], "unchanged")
        self.assertEqual(result["semantic_accuracy"], "unknown")
        self.assertEqual(result["authority"], "unknown")

    def test_equal_length_changed_bytes_are_unattributed(self):
        self.assertEqual(self.compare(observation(document_sha256=digest("modified")))["physical_retention"], "changed_unattributed")

    def test_expired_and_reversed_intervals_are_unknown(self):
        for time in ["2026-10-05T09:05:01+00:00", "2026-10-05T08:59:59+00:00"]:
            self.assertEqual(self.compare(observation(observed_at=time))["physical_retention"], "unknown")

    def test_cross_identity_and_person_are_unknown(self):
        for row in [observation(store_identity="other"), observation(person_key="other")]:
            self.assertEqual(self.compare(row)["physical_retention"], "unknown")

    def test_missing_or_failed_observation_is_unknown(self):
        for row in [{}, {"readback": "unknown", "error": "TimeoutExpired"}]:
            self.assertEqual(self.compare(row)["physical_retention"], "unknown")
        self.assertEqual(compare_inventory({}, {"alice": observation()})["alice"]["physical_retention"], "unknown")


if __name__ == "__main__":
    unittest.main()
