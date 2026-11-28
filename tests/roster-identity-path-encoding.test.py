#!/usr/bin/env python3
"""A stored evidence path names the codec that wrote it, or it is pre-codec.

The escape codec made `\\.` mean two different things. A path written before it
existed joined RAW segments with `.`, so `human\\.user_id` was the two segments
`['human\\', 'user_id']`; read as escaped it becomes the one segment
`human.user_id`, the repaired key is unreachable, and the carried refusal
latches past every repair. The string cannot disambiguate itself, so the RECORD
carries the discriminator and `decoded_path` is the only thing that reads it.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/collaboration-intelligence/scripts"
SCRIPT = SCRIPTS / "migrate_roster_identity.py"
sys.path.insert(0, str(SCRIPTS))
import roster_identity as ri  # noqa: E402
import migrate_roster_identity as mig  # noqa: E402

H = "1400000000000000001"
S = "1500000000000000002"
#: A roster key that is `human` followed by a literal backslash.
PARENT = "human\\"
PRE_CODEC = PARENT + ".user_id"                 # the parent writer's spelling
ESCAPED = ri.path_join([PARENT, "user_id"])     # this codec's spelling


def migrate(stored, parent=PARENT, leaf=None):
    """Run the production CLI over a v2 sidecar whose finding names `parent`
    and whose value at that path has since been repaired."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="path-enc-"))
    rp, out = d / "roster.json", d / "v2.json"
    rec = {"kind": "str", "reason": "held a value no id can be read from"}
    rec.update(stored)
    rp.write_text(json.dumps({
        "_schema": {"name": ri.SCHEMA_NAME, "version": ri.SCHEMA_VERSION,
                    "generated_at": "2026-09-01T00:00:00Z",
                    "migrated_from": "roster.json"},
        "alice": {parent: leaf if leaf is not None
                  else {"provider": "discord", "user_id": H},
                  ri.HUMAN_FIELD: H, ri.STAND_FIELD: S,
                  ri.SHAPE_FIELD: [rec]}}, ensure_ascii=False))
    r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(rp),
                        "--out", str(out)], capture_output=True, text=True)
    doc = json.loads(out.read_text()) if out.exists() else None
    return r.returncode, r.stdout + r.stderr, doc


class Codec(unittest.TestCase):
    def test_the_two_spellings_of_the_same_path_DIFFER(self):
        """The premise: if they were equal there would be nothing to decide."""
        self.assertNotEqual(PRE_CODEC, ESCAPED)
        self.assertEqual(ri.path_split(ESCAPED), [PARENT, "user_id"])

    def test_an_UNFLAGGED_path_is_read_as_pre_codec(self):
        self.assertEqual(ri.path_split(ri.decoded_path({"path": PRE_CODEC})),
                         [PARENT, "user_id"])

    def test_a_FLAGGED_path_is_read_as_escaped(self):
        rec = {"path": ESCAPED, ri.PATH_ENCODING_FIELD: ri.PATH_ENCODING}
        self.assertEqual(ri.path_split(ri.decoded_path(rec)), [PARENT, "user_id"])

    def test_an_ORDINARY_path_is_spelled_and_flagged_exactly_as_before(self):
        """No churn: escaping changes nothing here, so the pre-codec string is
        kept and no discriminator is written."""
        f = ri.path_fields(["human", "account", "user_id"])
        self.assertEqual(f, {"path": "human.account.user_id"})
        self.assertEqual(ri.path_split(ri.decoded_path(f)),
                         ["human", "account", "user_id"])

    def test_an_AMBIGUOUS_path_is_flagged(self):
        for segs in ([PARENT, "user_id"], ["discord.human", "user_id"]):
            with self.subTest(segments=segs):
                f = ri.path_fields(segs)
                self.assertEqual(f.get(ri.PATH_ENCODING_FIELD), ri.PATH_ENCODING)
                self.assertEqual(ri.path_split(ri.decoded_path(f)),
                                 [str(s) for s in segs])

    def test_decoding_is_IDEMPOTENT_across_a_re_migration(self):
        """A carried record is re-emitted every pass; decoding its own output
        again must not eat another backslash."""
        rec = dict(ri.path_fields([PARENT, "user_id"]), kind="str", reason="x")
        for _ in range(3):
            rec = ri.canonical_shape_failure(rec)
            self.assertEqual(ri.path_split(ri.decoded_path(rec)),
                             [PARENT, "user_id"])


class RepairReachability(unittest.TestCase):
    """The finding: only one spelling could reach the repaired key, so the
    refusal latched forever on a sidecar the parent commit wrote."""

    def test_a_PRE_CODEC_path_still_reaches_its_repaired_key(self):
        rc, err, doc = migrate({"path": PRE_CODEC})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"].get(ri.SHAPE_FIELD, []), [],
                         "the pre-codec path could not reach its own key")

    def test_a_FLAGGED_path_still_reaches_its_repaired_key(self):
        rc, err, doc = migrate({"path": ESCAPED,
                                ri.PATH_ENCODING_FIELD: ri.PATH_ENCODING})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"].get(ri.SHAPE_FIELD, []), [])

    def test_an_ORDINARY_path_still_reaches_its_repaired_key(self):
        """Control: the common case is unaffected in both directions."""
        rc, err, doc = migrate({"path": "human.account.user_id"}, parent="human",
                               leaf={"provider": "discord",
                                     "account": {"user_id": H}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["alice"].get(ri.SHAPE_FIELD, []), [])

    def test_an_UNREPAIRED_key_still_stays_refused(self):
        """Control: reachable and repaired are different questions, so a
        blanket "always clear" cannot satisfy the cases above."""
        for stored in ({"path": PRE_CODEC},
                       {"path": ESCAPED,
                        ri.PATH_ENCODING_FIELD: ri.PATH_ENCODING}):
            with self.subTest(stored=stored):
                rc, err, doc = migrate(stored, leaf={"provider": "discord",
                                                     "user_id": "not-an-id"})
                self.assertEqual(rc, 5, err)
                self.assertTrue(doc["alice"].get(ri.SHAPE_FIELD), err)

    def test_a_re_migration_keeps_the_finding_readable(self):
        """A pre-codec finding that is still live must survive the pass that
        re-spells it, with the discriminator its new spelling needs."""
        rc, err, doc = migrate({"path": PRE_CODEC},
                               leaf={"provider": "discord", "user_id": "nope"})
        self.assertEqual(rc, 5, err)
        rec = doc["alice"][ri.SHAPE_FIELD][0]
        self.assertEqual(rec.get(ri.PATH_ENCODING_FIELD), ri.PATH_ENCODING, rec)
        self.assertEqual(ri.path_split(ri.decoded_path(rec)),
                         [PARENT, "user_id"], rec)


#: A segment whose WORDS are all principal-container words, so it survives
#: eligibility, spelled two ways the two codecs disagree about.
AMBIGUOUS = ("human\\person", "human.person")


class EveryWriterStampsTheCodec(unittest.TestCase):
    """The invariant: a stored path must decode back to the segments the
    collector actually walked. A writer that emits the escaped spelling without
    the discriminator is read as pre-codec on the next pass and mis-splits."""

    def assert_round_trips(self, rec, segments):
        self.assertEqual(ri.path_split(ri.decoded_path(rec)),
                         [str(s) for s in segments], rec)

    def test_a_declared_slot_failure_round_trips(self):
        for seg in AMBIGUOUS:
            with self.subTest(segment=seg):
                shapes = []
                mig._collect_ids({seg: {"provider": "discord",
                                        "user_id": 12345}}, shapes)
                self.assertTrue(shapes)
                self.assert_round_trips(shapes[0], [seg, "user_id"])

    def test_a_NON_slot_typed_field_failure_round_trips(self):
        """The second emitter, reached only when the leaf is identity-bearing
        but declares no slot — its own stamp, not the other one's."""
        for seg in AMBIGUOUS:
            with self.subTest(segment=seg):
                shapes = []
                mig._collect_ids({seg: {"provider": "discord",
                                        "discord_human": 12345}}, shapes)
                self.assertTrue(shapes)
                self.assertIn("non-string", shapes[0]["reason"])
                self.assert_round_trips(shapes[0], [seg, "discord_human"])

    def test_a_SEED_round_trips_through_the_stored_record(self):
        """`seeded_by` stores paths the same way, so it needs the same stamp."""
        for seg in AMBIGUOUS:
            with self.subTest(segment=seg):
                seeds = mig._canonical_seeds(
                    [{"path": ri.path_join([ri.HUMAN_FIELD, seg, "user_id"]),
                      "verdict": "human", "reason": "seeded"}])
                self.assert_round_trips(seeds[0],
                                        [ri.HUMAN_FIELD, seg, "user_id"])


class PreCodecSeed(unittest.TestCase):
    """A seed path written before the codec is read the same way a carried
    finding is — through the record, not by re-parsing the string."""

    SEG = "human\\person"
    PRE = ri.HUMAN_FIELD + "." + SEG + ".user_id"

    def entry(self):
        return {ri.HUMAN_FIELD: {self.SEG: {"user_id": H}},
                ri.UNRESOLVED_FIELD: [
                    {"id": H, "reason": "sources disagree",
                     "seeded_by": [{"path": self.PRE, "verdict": "human",
                                    "reason": "seeded"}]}]}

    def test_the_two_readings_of_that_seed_path_DISAGREE(self):
        """The premise: re-parsing eats the backslash and loses the key."""
        self.assertTrue(mig._slot_erased(self.entry(), self.PRE))
        self.assertFalse(mig._slot_erased(
            self.entry(), ri.decoded_path({"path": self.PRE})))

    def test_a_REPAIRED_slot_discharges_a_pre_codec_seed(self):
        self.assertEqual(
            list(mig._carried_seeds(self.entry(), set(), {}, {}, "", 2)), [],
            "the repaired slot was unreachable, so the seed stayed latched")

    def test_an_ERASED_slot_still_carries_it(self):
        """Control: the discharge is the slot reading, not the decode."""
        e = self.entry()
        e[ri.HUMAN_FIELD] = None
        self.assertEqual(
            len(list(mig._carried_seeds(e, set(), {}, {}, "", 2))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
