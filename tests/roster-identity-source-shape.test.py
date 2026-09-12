#!/usr/bin/env python3
"""A source record whose shape the schema documents is REFUSED, never consumed.

Two measured ways a malformed source was blessed with a v2 map instead:
`discord-config.json` `owner` was checked only for `str`, so a padded `" H "`
matched no id and silently withdrew the human claim that makes `owner`+stand a
conflict; and a `people.<login>` that is not an object reached `tp.get(...)`
and crashed the CLI. Both publish -- or destroy -- an identity on evidence
nobody validated.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py"

H = "1400000000000000001"
T = "1600000000000000001"


def run(roster, triage=None, peers=None, dcfg=None):
    """-> (returncode, stderr, parsed output document or None)."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="src-shape-"))
    rp = d / "roster.json"
    rp.write_text(json.dumps(roster, ensure_ascii=False))
    args = [sys.executable, str(SCRIPT), "--roster", str(rp)]
    for flag, payload in (("--triage-config", triage), ("--peers", peers),
                          ("--discord-config", dcfg)):
        if payload is None:
            continue
        p = d / (flag.strip("-") + ".json")
        p.write_text(json.dumps(payload, ensure_ascii=False))
        args += [flag, str(p)]
    out = d / "v2.json"
    r = subprocess.run(args + ["--out", str(out)], capture_output=True, text=True)
    doc = json.loads(out.read_text()) if out.exists() else None
    return r.returncode, r.stdout + r.stderr, doc


class ExternalIdShape(unittest.TestCase):
    """`peers.json` values and `discord-config.json` `owner` are ids."""

    def test_a_PADDED_owner_is_refused_not_silently_unmatched(self):
        rc, err, doc = run({"alice": {"stand_status": "stand id " + H}},
                           dcfg={"owner": " " + H + " "})
        self.assertEqual(rc, 2, err)
        self.assertIsNone(doc, "a map was published on an unvalidated owner id")
        self.assertIn("owner", err)

    def test_the_UNPADDED_owner_still_conflicts_with_that_stand(self):
        """The behaviour the padded case must not be allowed to dodge."""
        rc, err, doc = run({"alice": {"stand_status": "stand id " + H}},
                           dcfg={"owner": H})
        self.assertEqual(rc, 5, err)
        self.assertIsNone(doc["alice"]["stand_discord_id"], err)

    def test_a_WELL_FORMED_owner_still_migrates(self):
        """Control: refusing every owner would satisfy the case above."""
        rc, err, doc = run({"alice": {"stand_status": "stand id " + H}},
                           dcfg={"owner": T})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"]["stand_discord_id"], H, err)

    def test_a_PADDED_peer_id_is_refused(self):
        rc, err, doc = run({"alice": {"stand_status": "stand id " + H}},
                           peers={"peer-bot": " " + T + " "})
        self.assertEqual(rc, 2, err)
        self.assertIsNone(doc)

    def test_a_WELL_FORMED_peer_id_still_migrates(self):
        rc, err, doc = run({"alice": {"stand_status": "stand id " + H}},
                           peers={"peer-bot": T})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"]["stand_discord_id"], H, err)


class PersonRecordShape(unittest.TestCase):
    """A person record is an object in both sources, or it is refused."""

    def test_a_triage_person_that_is_NOT_an_object_is_refused(self):
        rc, err, doc = run({"alice": {"gh": "alice", "stand_status": "stand id " + T}},
                           triage={"people": {"alice": "present-but-not-an-object"}})
        self.assertEqual(rc, 2, err)
        self.assertIsNone(doc, "a malformed triage record was blessed with v2")
        self.assertNotIn("Traceback", err, "the CLI crashed instead of refusing")

    def test_a_WELL_FORMED_triage_person_still_migrates(self):
        """Control: the refusal must be about the shape, not about triage."""
        rc, err, doc = run({"alice": {"gh": "alice"}},
                           triage={"people": {"alice": {"discord": H}}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"]["human_discord_id"], H, err)

    def test_a_roster_person_that_is_NOT_an_object_is_refused(self):
        rc, err, doc = run({"alice": "present-but-not-an-object"})
        self.assertEqual(rc, 2, err)
        self.assertIsNone(doc, "a non-object person row was copied into v2")

    def test_document_METADATA_still_passes_through_untouched(self):
        """Control: only a PERSON key is held to the person shape."""
        rc, err, doc = run({"_note": "not a person", "alice": {"stand_status": "stand id " + H}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["_note"], "not a person", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
