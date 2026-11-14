"""`--out` must not overwrite ANY supplied input, by name or by hardlink.

The shipped guard checked only `--roster`, so `--triage-config X --out X`
returned 0, replaced X's `people` map with the v2 roster, and printed
"input untouched" — the message a caller would have trusted.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py")

BOT = "111111111111111111"
# A shape the migration can actually RESOLVE: an unresolvable one exits 5 and
# the positive control then fails for a reason that has nothing to do with aliasing.
ROSTER = {"rui": {"github": "john-the-dev", "stand_status": f"stand id {BOT}"}}
TRIAGE = {"people": {"bob": {"discord": "333333333333333333"}}}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


class OutAlias(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.roster = self.d / "roster.json"
        self.triage = self.d / "triage.json"
        self.roster.write_text(json.dumps(ROSTER))
        self.triage.write_text(json.dumps(TRIAGE))

    def test_out_equal_to_the_roster_is_refused(self):
        r = run("--roster", self.roster, "--out", self.roster)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(self.roster.read_text()), ROSTER)

    def test_out_equal_to_the_TRIAGE_config_is_refused(self):
        before = self.triage.read_text()
        r = run("--roster", self.roster, "--triage-config", self.triage,
                "--out", self.triage)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.triage.read_text(), before,
                         "the auxiliary input was rewritten")

    def test_a_HARDLINK_to_an_input_is_refused(self):
        """A different name, the same inode: resolve() alone cannot see it."""
        link = self.d / "alias.json"
        os.link(self.triage, link)
        before = self.triage.read_text()
        r = run("--roster", self.roster, "--triage-config", self.triage, "--out", link)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.triage.read_text(), before)

    def test_a_NORMAL_sibling_output_still_succeeds(self):
        """The control: a guard that refuses everything is not a guard."""
        out = self.d / "roster.v2.json"
        r = run("--roster", self.roster, "--triage-config", self.triage, "--out", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(out.exists())
        self.assertEqual(json.loads(self.triage.read_text()), TRIAGE)


class ReMigrationKeepsARefusal(unittest.TestCase):
    """A re-migration must not turn a refusal into an authorization.

    The sidecar is fed back as input, which is what "re-migration" means here.
    Consumption asked `path_referent`, which reads the HEAD segment only, so a
    verdict derived from a NESTED segment failed the equality and was dropped:
    one verdict survived, the disagreement vanished, and the id resolved to a
    principal nobody agreed on. Measured rc 5 -> 0 -> 0 before the fix.
    """

    H = "1400000000000000001"
    S = "1500000000000000001"

    def three_passes(self, roster_doc):
        import json
        import subprocess
        import tempfile
        td = pathlib.Path(tempfile.mkdtemp())
        cur = td / "in0.json"
        cur.write_text(json.dumps(roster_doc, indent=1))
        script = (pathlib.Path(__file__).resolve().parent.parent
                  / "skills" / "collaboration-intelligence" / "scripts"
                  / "migrate_roster_identity.py")
        codes = []
        self.final = None
        for n in range(1, 4):
            out = td / f"sidecar{n}.json"
            r = subprocess.run([sys.executable, str(script), "--roster", str(cur),
                                "--out", str(out)], capture_output=True, text=True)
            codes.append(r.returncode)
            if not out.is_file():
                break
            self.final = json.loads(out.read_text())
            cur = out
        return codes

    def carried_seed(self, path, verdict):
        """A v2 sidecar whose ONLY evidence for H is a seed under `path`."""
        return {"_schema": {"name": "reviewer-identity", "version": 2},
                "reviewer": {"stand_status": self.S, "unresolved_discord_ids": [
                    {"id": self.H, "reason": "unclassified legacy id",
                     "seeded_by": [{"path": path, "verdict": verdict,
                                    "reason": "unverified carried statement"}]}]}}

    def assert_H_unpublished(self, codes):
        self.assertEqual(codes, [5, 5, 5],
                         f"a re-migration authorized an unbacked id: {codes}")
        e = self.final["reviewer"]
        self.assertNotEqual(e.get("human_discord_id"), self.H)
        self.assertNotIn(self.H, [(s.get("id") if isinstance(s, dict) else s)
                                  for s in e.get("other_stand_discord_ids") or []])
        self.assertIn(self.H, [(u.get("id") if isinstance(u, dict) else u)
                               for u in e.get("unresolved_discord_ids") or []])

    def test_a_nested_both_referent_path_under_a_STAND_root_stays_refused(self):
        codes = self.three_passes({"reviewer": {
            "stand_status": self.S,
            "other_stand_discord_ids": [
                {"human_profile": {"secondary_agent": {"id": self.H}}}],
        }})
        self.assertEqual(codes, [5, 5, 5],
                         f"re-migration resolved a real disagreement: {codes}")

    def test_a_nested_both_referent_path_under_a_HUMAN_root_stays_refused(self):
        codes = self.three_passes({"reviewer": {
            "human_discord_id": {"secondary_agent": {"id": self.H}},
            "stand_status": self.S,
        }})
        self.assertEqual(codes, [5, 5, 5],
                         f"re-migration resolved a real disagreement: {codes}")

    def test_a_carried_HUMAN_seed_under_the_UNRESOLVED_root_stays_refused(self):
        """Writer-owned is not eligible: `unresolved_discord_ids` is writer-owned
        and states no principal, yet its seed was consumed and H became the human.
        Measured rc 0 -> 0 -> 0 with `human_discord_id` = H before the fix."""
        self.assert_H_unpublished(self.three_passes(
            self.carried_seed("unresolved_discord_ids.human.id", "human")))

    def test_a_carried_STAND_seed_under_the_UNRESOLVED_root_stays_refused(self):
        self.assert_H_unpublished(self.three_passes(
            self.carried_seed("unresolved_discord_ids.stand.id", "stand")))

    def test_a_carried_seed_under_an_INELIGIBLE_nested_container_stays_refused(self):
        """Excluding only the unresolved root is too narrow: `room` is refused by
        the producer's container policy and must be refused by the consumer too."""
        self.assert_H_unpublished(self.three_passes(
            self.carried_seed("human_discord_id.room.stand.id", "stand")))

    def fresh_source(self, keys):
        """A v1 roster whose ONLY evidence for H sits under `human.<keys>.id`."""
        leaf = {"id": self.H}
        for k in reversed(keys):
            leaf = {k: leaf}
        return {"reviewer": {"stand_status": self.S, "human": leaf}}

    def test_a_fresh_BLANK_segment_between_principal_and_id_is_refused(self):
        """`_principal_slot` dropped blank segments before checking ancestors, so
        `human..account.id` was judged as `human.account.id`. Measured rc 0 with
        `human_discord_id` = H before the fix."""
        self.assert_H_unpublished(self.three_passes(self.fresh_source(["", "account"])))

    def test_a_fresh_WHITESPACE_segment_between_principal_and_id_is_refused(self):
        self.assert_H_unpublished(self.three_passes(self.fresh_source(["   ", "account"])))

    def test_a_carried_seed_with_a_BLANK_segment_stays_refused(self):
        """The consumer asks the same gate, so the same blank laundered a carried
        seed. Measured rc 0 -> 0 -> 0 with `human_discord_id` = H before the fix."""
        self.assert_H_unpublished(self.three_passes(
            self.carried_seed("human_discord_id..human.id", "human")))

    BOTH = "human_discord_id.secondary_agent.id"

    def test_a_carried_BOTH_referent_path_with_only_the_HUMAN_seed_stays_refused(self):
        """The path states both referents; one stored seed must not pick one.
        Measured rc 0 -> 0 -> 0 with `human_discord_id` = H before the fix."""
        self.assert_H_unpublished(self.three_passes(self.carried_seed(self.BOTH, "human")))

    def test_a_carried_BOTH_referent_path_with_only_the_STAND_seed_stays_refused(self):
        """Measured rc 0 -> 0 -> 0 with H among the Stands before the fix."""
        self.assert_H_unpublished(self.three_passes(self.carried_seed(self.BOTH, "stand")))

    def test_CONTROL_a_carried_BOTH_referent_path_with_both_seeds_stays_refused(self):
        doc = self.carried_seed(self.BOTH, "human")
        doc["reviewer"]["unresolved_discord_ids"][0]["seeded_by"].append(
            {"path": self.BOTH, "verdict": "stand", "reason": "unverified carried statement"})
        self.assert_H_unpublished(self.three_passes(doc))

    def flat_source(self, key):
        """A v1 roster whose ONLY evidence for H is the flat field `key`."""
        return {"reviewer": {"stand_status": self.S, key: self.H}}

    def test_a_fresh_NON_ASCII_object_token_in_a_HUMAN_leaf_is_refused(self):
        """`_WORDS` matched ASCII letters only, so `对象` vanished and the leaf
        read as `discord_human_id`. Measured rc 0 with `human_discord_id` = H
        before the fix; the ASCII control below refused all along."""
        self.assert_H_unpublished(self.three_passes(self.flat_source("discord_human_对象_id")))

    def test_a_fresh_NON_ASCII_object_token_in_a_STAND_leaf_is_refused(self):
        codes = self.three_passes(self.flat_source("discord_stand_对象_id"))
        self.assertEqual(codes, [5, 5, 5], codes)
        e = self.final["reviewer"]
        self.assertNotIn(self.H, [(s.get("id") if isinstance(s, dict) else s)
                                  for s in e.get("other_stand_discord_ids") or []])

    def test_a_carried_seed_with_a_NON_ASCII_object_container_stays_refused(self):
        self.assert_H_unpublished(self.three_passes(
            self.carried_seed("human_discord_id.human_对象.id", "human")))

    def test_CONTROL_an_ASCII_unknown_object_token_is_refused(self):
        self.assert_H_unpublished(self.three_passes(self.flat_source("discord_human_integration_id")))

    def test_CONTROL_a_flat_documented_human_leaf_still_resolves(self):
        codes = self.three_passes(self.flat_source("discord_human_id"))
        self.assertEqual(codes, [0, 0, 0], codes)
        self.assertEqual(self.final["reviewer"].get("human_discord_id"), self.H)

    SHAPE = "id_shape_failures"

    def three_passes_with_triage(self, roster_doc, triage):
        td = pathlib.Path(tempfile.mkdtemp())
        (td / "triage.json").write_text(json.dumps(triage))
        cur = td / "in0.json"
        cur.write_text(json.dumps(roster_doc, indent=1))
        codes = []
        self.final = None
        for n in range(1, 4):
            out = td / f"sidecar{n}.json"
            r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(cur),
                                "--triage-config", str(td / "triage.json"), "--out", str(out)],
                               capture_output=True, text=True)
            codes.append(r.returncode)
            if not out.is_file():
                break
            self.final = json.loads(out.read_text())
            cur = out
        return codes

    def pathless_shape(self, kind, states, ids=None):
        """A v2 sidecar whose ONLY record of H is a pathless shape finding."""
        rec = {"path": None, "kind": kind, "reason": "carried contest",
               "arbitrated_ids": [self.H] if ids is None else ids,
               "arbitrated_states": states}
        return {"_schema": {"name": "reviewer-identity", "version": 2},
                "reviewer": {"stand_status": self.S, self.SHAPE: [rec]}}

    TRIAGE_HUMAN = {"people": {"reviewer": {"discord": "1400000000000000001"}}}
    TRIAGE_BOT = {"people": {"reviewer": {"bots": ["1400000000000000001"]}}}

    def test_a_pathless_identity_record_is_kept_and_the_triage_HUMAN_stays_contested(self):
        """`_still_unresolved` kept only two named kinds and dropped every other
        pathless record, so a `list` finding naming H as a Stand vanished and the
        triage config then published H as the human. Measured rc 0 -> 0 -> 0 with
        `human_discord_id` = H and the record deleted before the fix."""
        codes = self.three_passes_with_triage(self.pathless_shape("list", ["stand"]), self.TRIAGE_HUMAN)
        self.assertEqual(codes, [5, 5, 5], codes)
        e = self.final["reviewer"]
        self.assertNotEqual(e.get("human_discord_id"), self.H)
        self.assertEqual(len(e.get(self.SHAPE) or []), 1, "the identity record was erased")

    def test_a_pathless_identity_record_is_kept_and_the_triage_STAND_stays_contested(self):
        codes = self.three_passes_with_triage(self.pathless_shape("list", ["human"]), self.TRIAGE_BOT)
        self.assertEqual(codes, [5, 5, 5], codes)
        e = self.final["reviewer"]
        self.assertNotIn(self.H, [(s.get("id") if isinstance(s, dict) else s)
                                  for s in e.get("other_stand_discord_ids") or []])
        self.assertEqual(len(e.get(self.SHAPE) or []), 1)

    def test_CONTROL_the_overflow_kind_was_always_kept(self):
        codes = self.three_passes_with_triage(
            self.pathless_shape("arbitration-overflow", ["stand"]), self.TRIAGE_HUMAN)
        self.assertEqual(codes, [5, 5, 5], codes)

    def test_CONTROL_a_pathless_diagnostic_with_no_ids_is_still_dropped(self):
        """The predicate is identity-bearing, not carry-everything."""
        codes = self.three_passes_with_triage(self.pathless_shape("list", [], ids=[]), self.TRIAGE_HUMAN)
        self.assertEqual(codes, [0, 0, 0], codes)
        e = self.final["reviewer"]
        self.assertEqual(e.get("human_discord_id"), self.H)
        self.assertEqual(len(e.get(self.SHAPE) or []), 0)

    def test_a_SCALAR_snowflake_in_arbitrated_ids_is_one_contested_id_HUMAN_direction(self):
        """`_snowflake_list` turned a whole scalar snowflake into [], so the
        record lost its identity fact and was dropped; the triage config then
        published H. Measured rc 0 -> 0 -> 0 with `human_discord_id` = H before."""
        codes = self.three_passes_with_triage(self.pathless_shape("list", ["stand"], ids=self.H), self.TRIAGE_HUMAN)
        self.assertEqual(codes, [5, 5, 5], codes)
        e = self.final["reviewer"]
        self.assertNotEqual(e.get("human_discord_id"), self.H)
        self.assertEqual([r.get("arbitrated_ids") for r in e.get(self.SHAPE) or []], [[self.H]])

    def test_a_SCALAR_snowflake_in_arbitrated_ids_is_one_contested_id_STAND_direction(self):
        codes = self.three_passes_with_triage(self.pathless_shape("list", ["human"], ids=self.H), self.TRIAGE_BOT)
        self.assertEqual(codes, [5, 5, 5], codes)
        e = self.final["reviewer"]
        self.assertNotIn(self.H, [(s.get("id") if isinstance(s, dict) else s)
                                  for s in e.get("other_stand_discord_ids") or []])

    def test_a_PRESENT_but_unreadable_arbitrated_ids_blocks_instead_of_vanishing(self):
        """Present-but-unusable is a refusal, never an absence (schema)."""
        codes = self.three_passes_with_triage(self.pathless_shape("list", ["stand"], ids="not-an-id"), self.TRIAGE_HUMAN)
        self.assertEqual(codes, [5, 5, 5], codes)
        kinds = [r.get("kind") for r in self.final["reviewer"].get(self.SHAPE) or []]
        self.assertIn("invalid", kinds, kinds)

    def test_CONTROL_the_documented_account_container_still_resolves(self):
        """The fix must refuse the BLANK, not the container: `account` is documented."""
        codes = self.three_passes(self.fresh_source(["account"]))
        self.assertEqual(codes, [0, 0, 0])
        self.assertEqual(self.final["reviewer"].get("human_discord_id"), self.H)

    def test_CONTROL_an_unknown_container_is_still_refused(self):
        self.assert_H_unpublished(self.three_passes(
            self.fresh_source(["zzz_unheard_of", "account"])))

    def test_CONTROL_a_single_referent_slot_still_migrates_cleanly(self):
        """Without this the fix could pass by refusing everything forever."""
        self.assertEqual(
            self.three_passes({"reviewer": {"other_stand_discord_ids": [self.S]}}),
            [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
