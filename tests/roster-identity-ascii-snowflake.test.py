#!/usr/bin/env python3
"""A snowflake is ASCII digits. `\\d` accepts every Unicode decimal digit, so a
19-character Arabic-Indic string satisfied `\\d{17,20}` and travelled as an
authoritative id through the coherence gate and every accessor.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills/collaboration-intelligence/scripts"


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / fn)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ri = _load("ri_ascii", "roster_identity.py")
mri = _load("mri_ascii", "migrate_roster_identity.py")

ASCII = "1400000000000000001"
ARABIC = "١٤٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠١"


class ASCIIOnlySnowflakes(unittest.TestCase):
    def test_the_fixture_is_in_range_for_the_OLD_pattern(self):
        """Without this the suite could pass on a fixture never in range."""
        import re
        self.assertEqual(len(ARABIC), 19)
        self.assertTrue(re.fullmatch(r"\d{17,20}", ARABIC),
                        "fixture must satisfy the pattern being replaced")

    def test_ascii_still_resolves(self):
        self.assertTrue(ri.is_snowflake(ASCII))
        self.assertEqual(mri._SNOWFLAKE.findall(ASCII), [ASCII])

    def test_a_unicode_digit_string_is_not_an_id(self):
        self.assertFalse(ri.is_snowflake(ARABIC))

    def test_a_mixed_run_is_rejected_WHOLE_not_trimmed_to_its_ascii_tail(self):
        # Boundaries stay `\\d` on purpose: an ASCII tail adjacent to a Unicode
        # digit must not be extracted as an authoritative id.
        self.assertEqual(mri._SNOWFLAKE.findall("١" + ASCII), [])
        self.assertFalse(ri.is_snowflake("١" + ASCII[1:]))

    def test_the_plural_accessor_drops_a_unicode_member(self):
        e = {"_schema": "reviewer-identity/2", "human_discord_id": ASCII,
             "stand_discord_id": "1500000000000000001",
             ri.OTHER_STANDS_FIELD: [{"id": ARABIC}]}
        self.assertFalse(ri.entry_is_coherent(e))
        self.assertEqual(ri.stand_discord_ids(e), [])


class EvidenceIsMinedFromSTRUCTURE(unittest.TestCase):
    """Ids are read from a value\'s scalar leaves, never from its JSON text.

    `json.dumps` renders a non-ASCII digit as a `\\uXXXX` escape whose hex
    digits then read as part of an id present in no source.
    """

    FULLWIDTH = "\uff11" + "1" * 15

    def test_the_fixture_names_NO_id_of_its_own(self):
        self.assertEqual(len(self.FULLWIDTH), 16)
        self.assertEqual(mri._SNOWFLAKE.findall(self.FULLWIDTH), [])

    def test_the_fixture_DOES_mint_one_once_json_escaped(self):
        """Without this the suite could pass on a fixture that never fooled it."""
        import json as _json
        self.assertEqual(len(mri._SNOWFLAKE.findall(_json.dumps(self.FULLWIDTH))), 1)

    def test_the_structured_scan_invents_nothing(self):
        self.assertEqual(mri._structured_snowflakes(self.FULLWIDTH), [])
        self.assertEqual(mri._structured_snowflakes({"discord": self.FULLWIDTH}), [])

    def test_a_value_that_really_NAMES_an_id_is_still_reported(self):
        """Control: returning nothing always would satisfy the case above."""
        self.assertEqual(mri._structured_snowflakes("id=" + ASCII), [ASCII])
        self.assertEqual(mri._structured_snowflakes({"bot": ASCII}), [ASCII])
        self.assertEqual(mri._structured_snowflakes([{"a": [ASCII]}]), [ASCII])

    def test_the_CLI_publishes_no_invented_id(self):
        import json as _json
        import subprocess
        import tempfile
        d = Path(tempfile.mkdtemp())
        roster = d / "roster.json"
        triage = d / "triage.json"
        out = d / "v2.json"
        roster.write_text(_json.dumps({"alice": {"gh": "alice",
                                                 "stand_status": "stand id " + ASCII}}))
        triage.write_text(_json.dumps({"people": {"alice": {"discord": self.FULLWIDTH}}},
                                      ensure_ascii=False))
        r = subprocess.run([sys.executable, str(SCRIPTS / "migrate_roster_identity.py"),
                            "--roster", str(roster), "--triage-config", str(triage),
                            "--out", str(out)], capture_output=True, text=True)
        self.assertTrue(out.exists(), r.stdout + r.stderr)
        doc = _json.loads(out.read_text())
        self.assertEqual([u.get("id") for u in doc["alice"]["unresolved_discord_ids"]],
                         [], "an id absent from every source was published")
        self.assertEqual(doc["alice"]["stand_discord_id"], ASCII)


if __name__ == "__main__":
    unittest.main(verbosity=2)
