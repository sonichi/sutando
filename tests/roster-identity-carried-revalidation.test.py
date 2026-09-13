"""Carried-refusal revalidation must ask the COLLECTOR, not a second parser.

`_still_unresolved` used `_mineable_now`, which scanned raw JSON for digits with
none of the collector's path, provider or identity-leaf rules, and walked the
finding's path through dicts only. Two consequences, in opposite directions:
an irrelevant snowflake in unreadable metadata cleared a real refusal, and a
repair on a documented `identities[]` path could never clear one.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "skills/collaboration-intelligence/scripts"))
import migrate_roster_identity as m  # noqa: E402

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py")

H = "1400000000000000001"
T = "1600000000000000001"


def rec(path):
    return {"path": path, "kind": "unreadable", "reason": "x"}


class EvidencePathCodec(unittest.TestCase):
    """A path is JOINED by the collector and SPLIT by the revalidator, so one
    codec owns both. A roster key may itself contain the separator."""

    def test_an_ordinary_path_keeps_its_existing_spelling(self):
        """Back-compat control: every stored path is dot-joined and unescaped,
        and must keep parsing into exactly the segments it always did."""
        self.assertEqual(m._path_join(["human", "account", "user_id"]),
                         "human.account.user_id")
        self.assertEqual(m._path_split("human.account.user_id"),
                         ["human", "account", "user_id"])

    def test_a_segment_containing_the_separator_ROUND_TRIPS(self):
        self.assertEqual(m._path_split(m._path_join(["discord.human.user_id"])),
                         ["discord.human.user_id"])

    def test_a_repair_under_a_FLAT_dotted_key_can_be_finished(self):
        """Split as four segments the key was unreachable, so `_still_unresolved`
        answered "cannot re-check" and latched the refusal past every repair."""
        e = {"discord.human.user_id": H}
        self.assertEqual(m._collect_ids(e), [H])
        self.assertFalse(m._still_unresolved(
            e, rec(m._path_join(["discord.human.user_id"])), set()))

    def test_a_FLAT_dotted_key_still_holding_junk_stays_refused(self):
        """Control: reachable and readable are different questions."""
        e = {"discord.human.user_id": "still-not-an-id"}
        self.assertTrue(m._still_unresolved(
            e, rec(m._path_join(["discord.human.user_id"])), set()))


class CarriedRevalidation(unittest.TestCase):
    def test_an_irrelevant_snowflake_in_metadata_cannot_clear(self):
        """`_collect_ids({'stand_status': {'note': T}})` is [], so T is not an
        id the migration would ever read; it must not retire the finding."""
        self.assertEqual(m._collect_ids({"stand_status": {"note": T}}), [])
        self.assertTrue(m._still_unresolved({"stand_status": {"note": T}},
                                            rec("stand_status"), set()))

    def test_a_repair_on_a_LIST_path_clears(self):
        """A list does not consume a path segment — the rule the collector
        applies. A dict-only descent made this path permanently unreachable,
        so the repair could never retire the refusal."""
        e = {"human": {"provider": "discord", "identities": [{"user_id": H}]}}
        self.assertEqual(m._collect_ids(e), [H])
        self.assertFalse(m._still_unresolved(e, rec("human.identities.user_id"), set()))

    def test_the_PROVIDER_is_carried_down_the_path(self):
        """Detached from the dict that declares it, an identity leaf stops being
        Discord and every repair reads as still-broken. Same value, provider
        removed, is not readable and must stay refused — that pair is the test."""
        with_p = {"human": {"provider": "discord", "account": {"user_id": H}}}
        without = {"human": {"account": {"user_id": H}}}
        self.assertEqual(m._collect_ids(with_p), [H])
        self.assertEqual(m._collect_ids(without), [])
        self.assertFalse(m._still_unresolved(with_p, rec("human.account.user_id"), set()))
        self.assertTrue(m._still_unresolved(without, rec("human.account.user_id"), set()))

    def test_a_NON_id_at_a_readable_path_stays_refused(self):
        """Control: the fix must not clear a path merely because it is now
        reachable. Reachable and readable are different questions."""
        e = {"human": {"provider": "discord", "identities": [{"user_id": "nope"}]}}
        self.assertTrue(m._still_unresolved(e, rec("human.identities.user_id"), set()))

    def test_blank_and_absent_still_refuse(self):
        """Controls on the two pre-existing branches, so the rewrite cannot
        quietly drop them."""
        blank = {"human": {"provider": "discord", "account": {"user_id": "  "}}}
        self.assertTrue(m._still_unresolved(blank, rec("human.account.user_id"), set()))
        self.assertTrue(m._still_unresolved({}, rec("no.such.path"), set()))

    def test_our_OWN_slots_survive_a_foreign_provider_on_the_row(self):
        """`human_discord_id` is this schema's field and names Discord itself.
        A source-declared `provider` describes the SOURCE namespace; letting it
        shadow the writer\'s slots made a v2 row unable to re-read its own ids."""
        e = {"provider": "matrix", "human_discord_id": H}
        self.assertEqual(m._collect_ids(e), [H])

    def test_a_foreign_provider_still_blocks_a_SOURCE_leaf(self):
        """Control: the exemption is the writer-owned slots, not every leaf."""
        self.assertEqual(
            m._collect_ids({"provider": "matrix", "human": {"user_id": H}}), [])

    def test_the_second_parser_is_GONE(self):
        """Delegation is the point; a surviving copy would drift again."""
        self.assertFalse(hasattr(m, "_mineable_now"))


class ThroughTheCLI(unittest.TestCase):
    """A migration whose output cannot be re-migrated erases what it published."""

    def _run(self, d, roster_path, triage=None, tag="v2"):
        args = [sys.executable, str(SCRIPT), "--roster", str(roster_path)]
        if triage is not None:
            p = d / ("triage-%s.json" % tag)
            p.write_text(json.dumps(triage))
            args += ["--triage-config", str(p)]
        out = d / (tag + ".json")
        r = subprocess.run(args + ["--out", str(out)], capture_output=True, text=True)
        return r, (json.loads(out.read_text()) if out.exists() else None)

    def test_a_row_with_a_FOREIGN_provider_re_reads_its_own_published_ids(self):
        d = pathlib.Path(tempfile.mkdtemp())
        src = d / "roster.json"
        src.write_text(json.dumps({"alice": {"gh": "alice", "provider": "matrix"}}))
        triage = {"people": {"alice": {"discord": H, "bots": [T]}}}
        r1, doc1 = self._run(d, src, triage=triage, tag="v2")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual((doc1["alice"]["human_discord_id"],
                          doc1["alice"]["stand_discord_id"]), (H, T))
        r2, doc2 = self._run(d, d / "v2.json", tag="v3")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual((doc2["alice"]["human_discord_id"],
                          doc2["alice"]["stand_discord_id"]), (H, T),
                         "re-migrating the v2 map erased its own ids")

    def test_a_repaired_FLAT_key_finishes_the_repair(self):
        d = pathlib.Path(tempfile.mkdtemp())
        src = d / "roster.json"
        src.write_text(json.dumps({"alice": {"gh": "alice",
                                             "discord.human.user_id": "12"}}))
        r1, doc1 = self._run(d, src, tag="v2")
        self.assertEqual(r1.returncode, 5, r1.stderr)
        self.assertTrue(doc1["alice"].get("id_shape_failures"))
        doc1["alice"]["discord.human.user_id"] = H
        rep = d / "repaired.json"
        rep.write_text(json.dumps(doc1))
        r2, doc2 = self._run(d, rep, tag="v3")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(doc2["alice"]["human_discord_id"], H)
        self.assertFalse(doc2["alice"].get("id_shape_failures"))

    def test_a_FLAT_key_repaired_to_JUNK_stays_refused(self):
        """Control: the repair must clear on the VALUE, not on reachability."""
        d = pathlib.Path(tempfile.mkdtemp())
        src = d / "roster.json"
        src.write_text(json.dumps({"alice": {"gh": "alice",
                                             "discord.human.user_id": "12"}}))
        _r1, doc1 = self._run(d, src, tag="v2")
        doc1["alice"]["discord.human.user_id"] = "still-not-an-id"
        rep = d / "repaired.json"
        rep.write_text(json.dumps(doc1))
        r2, _doc2 = self._run(d, rep, tag="v3")
        self.assertEqual(r2.returncode, 5, r2.stdout + r2.stderr)


if __name__ == "__main__":
    unittest.main()
