#!/usr/bin/env python3
"""Writer ownership is a property of the PATH, not of a leaf's spelling.

Matching the leaf text against the writer-owned field names fails both ways at
once. A descendant of our own plural slot -- `other_stand_discord_ids[].id` --
does not spell a writer-owned name, so an ambient foreign `provider` hid it and
a clean v2 output lost that Stand on its next pass. The inverse is worse: a
SOURCE object spelling `human_discord_id` under `provider: matrix` bypassed the
provider and published a Matrix identifier as the authoritative Discord human.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py"
sys.path.insert(0, str(ROOT / "skills/collaboration-intelligence/scripts"))
import roster_identity as ri  # noqa: E402

H = "1358841611580080161"
S = "1358841611580080162"
T = "1358841611580080163"


def run(roster, triage=None, out_name="v2.json", d=None):
    """-> (returncode, parsed output document or None, directory)."""
    d = d or pathlib.Path(tempfile.mkdtemp(prefix="wop-"))
    rp = d / "roster.json" if isinstance(roster, dict) else roster
    if isinstance(roster, dict):
        rp.write_text(json.dumps(roster, ensure_ascii=False))
    args = [sys.executable, str(SCRIPT), "--roster", str(rp)]
    if triage is not None:
        tp = d / "triage.json"
        tp.write_text(json.dumps(triage, ensure_ascii=False))
        args += ["--triage-config", str(tp)]
    out = d / out_name
    r = subprocess.run(args + ["--out", str(out)], capture_output=True, text=True)
    doc = json.loads(out.read_text()) if out.exists() else None
    return r.returncode, doc, d


class APluralDescendantSurvivesReMigration(unittest.TestCase):
    """The two-pass bound: our own output must migrate to itself."""

    def _pass_one(self):
        rc, doc, d = run({"p": {"provider": "matrix"}},
                         {"people": {"p": {"discord": H, "bots": [S, T]}}})
        self.assertEqual(rc, 0, "pass 1 must publish")
        rec = doc["p"]
        self.assertEqual(rec[ri.HUMAN_FIELD], H)
        self.assertEqual(rec[ri.STAND_FIELD], S)
        self.assertEqual(ri.stand_discord_ids(rec), [S, T],
                         "pass 1 must carry the secondary Stand")
        return d

    def test_a_second_pass_keeps_the_secondary_stand(self):
        d = self._pass_one()
        rc, doc, _ = run(d / "v2.json", out_name="v2b.json", d=d)
        rec = doc["p"]
        self.assertEqual(ri.stand_discord_ids(rec), [S, T],
                         "the plural slot's own `id` must be readable beneath "
                         "an ambient foreign provider")
        self.assertEqual(rec.get(ri.SHAPE_FIELD, []), [],
                         "a readable slot must raise no shape failure")
        self.assertEqual(rc, 0, "a complete re-migration is not a coverage gap")

    def test_a_second_pass_with_the_same_triage_is_also_clean(self):
        d = self._pass_one()
        rc, doc, _ = run(d / "v2.json",
                         {"people": {"p": {"discord": H, "bots": [S, T]}}},
                         out_name="v2c.json", d=d)
        self.assertEqual(doc["p"].get(ri.SHAPE_FIELD, []), [])
        self.assertEqual(rc, 0)


class ANestedSchemaSpellingDoesNotLaunderAForeignId(unittest.TestCase):
    """A schema field is canonical at the documented TOP LEVEL only."""

    def _human(self, provider, field):
        rc, doc, _ = run({"p": {"account": {"provider": provider, field: H}}})
        return rc, ri.human_discord_id(doc["p"])

    def test_a_matrix_id_under_a_nested_canonical_spelling_is_refused(self):
        _rc, human = self._human("matrix", ri.HUMAN_FIELD)
        self.assertIsNone(human, "a nested spelling must not bypass the provider")

    def test_the_source_spelling_under_matrix_is_refused_identically(self):
        # The two spellings must agree; the canonical one was the outlier.
        _rc, human = self._human("matrix", "discord_human_id")
        self.assertIsNone(human)

    def test_a_nested_discord_provider_still_resolves(self):
        # The guard against satisfying the case above by refusing everything.
        rc, human = self._human("discord", ri.HUMAN_FIELD)
        self.assertEqual(human, H)
        self.assertEqual(rc, 0)


class ARootWriterOwnedSlotStillBeatsAForeignProvider(unittest.TestCase):
    """The exception is narrowed to the top level, not deleted."""

    def test_a_root_canonical_field_outranks_an_ambient_provider(self):
        rc, doc, _ = run({"p": {"provider": "matrix", ri.HUMAN_FIELD: H,
                                ri.STAND_FIELD: S}})
        self.assertEqual(ri.human_discord_id(doc["p"]), H)
        self.assertEqual(ri.stand_discord_id(doc["p"]), S)
        self.assertEqual(rc, 0)


class TheSchemaOwnsPathEligibility(unittest.TestCase):
    """The migrator must ask the schema, not re-derive ownership."""

    def test_segments_decide_from_the_head(self):
        self.assertTrue(ri.writer_owned_segments([ri.OTHER_STANDS_FIELD, "id"]))
        self.assertTrue(ri.writer_owned_segments([ri.HUMAN_FIELD]))
        self.assertFalse(ri.writer_owned_segments(["account", ri.HUMAN_FIELD]))
        self.assertFalse(ri.writer_owned_segments([]))

    def test_the_joined_path_reader_delegates_to_the_segment_owner(self):
        import migrate_roster_identity as mig
        orig = ri.writer_owned_segments
        seen = []
        ri.writer_owned_segments = lambda segs: seen.append(list(segs)) or False
        try:
            ri.writer_owned_path(ri.HUMAN_FIELD)
            mig._discord_source([ri.OTHER_STANDS_FIELD], "id", "matrix")
        finally:
            ri.writer_owned_segments = orig
        self.assertIn([ri.HUMAN_FIELD], seen, "path_split reader must delegate")
        self.assertIn([ri.OTHER_STANDS_FIELD, "id"], seen,
                      "the collector must ask the schema with full segments")


if __name__ == "__main__":
    unittest.main(verbosity=2)
