#!/usr/bin/env python3
"""A person and their agent must not be interchangeable in the identity map.

Measured 2026-08-28 on the live stores: the roster's `discord_id` for
qingyun-wu held 1504316176686120980 (the AGENT) while pr-triage's `discord`
for the same login held 1025828152183885925 (the HUMAN). Both stores spell the
field "discord". A merge that trusts that name resolves a human lookup to a bot
id, and every downstream ping then reaches the wrong party while reporting
success — the failure is invisible at the call site.

So the schema names the referent (`human_discord_id` / `stand_discord_id`), the
migration classifies only from a source that states which one an id is, and a
pre-migration document is refused rather than answered approximately.

Run: python3 tests/roster-identity-migration.test.py
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parent.parent / "skills"
           / "collaboration-intelligence" / "scripts")

HUMAN = "1025828152183885925"
BOT = "1504316176686120980"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ri = _load("roster_identity")
mig = _load("migrate_roster_identity")


def migrate(doc, triage=None, peers=None, owner=""):
    return mig.migrate(doc, triage or {}, peers or {}, owner, "test.json")


class BotIdNeverAnswersAHumanLookup(unittest.TestCase):
    """The negative that matters. Broken = a bot id reachable as a person."""

    def setUp(self):
        # The live v1 shape, verbatim in structure: the bare `discord_id` is the
        # AGENT and only `discord_human_id` names the person.
        self.v1 = {"qingyun-wu": {
            "discord_id": BOT,
            "discord_stand_name": "qingyun-sutando#1831",
            "discord_human_id": HUMAN,
            "stand_status": f"ADDRESSABLE VIA DISCORD: stand id {BOT}.",
        }}
        self.out, _ = migrate(
            self.v1, {"qingyun-wu": {"discord": HUMAN, "bots": [BOT]}})
        self.entry = self.out["qingyun-wu"]

    def test_human_lookup_returns_the_person(self):
        self.assertEqual(ri.human_discord_id(self.entry), HUMAN)

    def test_human_lookup_never_returns_the_agent(self):
        """Fails the moment `discord_id` is allowed to answer a human lookup."""
        self.assertNotEqual(ri.human_discord_id(self.entry), BOT)

    def test_agent_lookup_returns_the_agent(self):
        self.assertEqual(ri.stand_discord_id(self.entry), BOT)

    def test_the_two_slots_never_hold_the_same_id(self):
        self.assertNotEqual(ri.human_discord_id(self.entry),
                            ri.stand_discord_id(self.entry))

    def test_v1_field_alone_cannot_reach_the_human_slot(self):
        """With `discord_human_id` and the triage config removed, the id that
        remains is the agent's — and it must NOT be promoted to the human."""
        stripped = {"qingyun-wu": {"discord_id": BOT,
                                   "stand_status": f"stand id {BOT}"}}
        entry = migrate(stripped)[0]["qingyun-wu"]
        self.assertIsNone(ri.human_discord_id(entry))
        self.assertEqual(ri.stand_discord_id(entry), BOT)


class MigrationDoesNotGuess(unittest.TestCase):
    def test_unclassifiable_id_lands_in_neither_slot(self):
        entry = migrate({"x": {"discord_id": "1490412828065267872",
                               "display_name": "Sutando-Mini"}})[0]["x"]
        self.assertIsNone(ri.human_discord_id(entry))
        self.assertIsNone(ri.stand_discord_id(entry))
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(entry)],
                         ["1490412828065267872"])

    def test_a_display_name_is_not_evidence(self):
        """"Sutando-Mini" reads as a bot to a person and states nothing to a
        program. Broken = the name is treated as a classification."""
        entry = migrate({"x": {"discord_id": "1490412828065267872",
                               "display_name": "Sutando-Mini",
                               "principal": "peer bot node"}})[0]["x"]
        self.assertIsNone(ri.stand_discord_id(entry))

    def test_disagreeing_sources_are_unresolved_not_arbitrated(self):
        doc = {"x": {"discord_human_id": BOT}}
        entry = migrate(doc, {"x": {"discord": "999", "bots": [BOT]}})[0]["x"]
        ids = [u["id"] for u in ri.unresolved_discord_ids(entry)]
        self.assertIn(BOT, ids)
        self.assertIsNone(ri.stand_discord_id(entry))

    def test_a_second_agent_does_not_displace_the_first(self):
        doc = {"kewei": {"discord_id": "1537956198618243162",
                         "stand_status": "stand id 1537956198618243162",
                         "secondary_agent": {"discord_id": "1529720369668292629"}}}
        entry = migrate(doc)[0]["kewei"]
        self.assertEqual(ri.stand_discord_ids(entry),
                         ["1537956198618243162", "1529720369668292629"])
        self.assertIsNone(ri.human_discord_id(entry))


class EvidenceSources(unittest.TestCase):
    def test_triage_config_names_both_referents(self):
        out, _ = migrate({"john": {}}, {"john": {"discord": HUMAN, "bots": []}})
        self.assertEqual(ri.human_discord_id(out["john"]), HUMAN)

    def test_peers_file_classifies_a_peer_bot(self):
        out, _ = migrate({"pro": {"discord_id": "150932"+"9143110565888"}},
                         peers={"1509329143110565888": "pro"})
        self.assertEqual(ri.stand_discord_id(out["pro"]), "1509329143110565888")

    def test_discord_config_owner_classifies_the_human(self):
        out, _ = migrate({"chi": {"discord_id": "1022910063620390932"}},
                         owner="1022910063620390932")
        self.assertEqual(ri.human_discord_id(out["chi"]), "1022910063620390932")
        self.assertIsNone(ri.stand_discord_id(out["chi"]))

    def test_every_classified_id_carries_its_basis(self):
        out, _ = migrate({"chi": {"discord_id": "1022910063620390932"}},
                         owner="1022910063620390932")
        self.assertTrue(out["chi"][ri.BASIS_FIELD][ri.HUMAN_FIELD])


class SchemaGate(unittest.TestCase):
    def test_a_v1_document_is_refused_not_answered(self):
        with self.assertRaises(ValueError):
            ri.require_v2({"qingyun-wu": {"discord_id": BOT}})

    def test_a_migrated_document_passes(self):
        out, _ = migrate({"x": {}})
        self.assertEqual(ri.schema_version(out), ri.SCHEMA_VERSION)
        ri.require_v2(out)

    def test_metadata_keys_are_not_people(self):
        out, _ = migrate({"x": {}, "_merge_log": ["kept"]})
        self.assertEqual(list(ri.people(out)), ["x"])
        self.assertEqual(out["_merge_log"], ["kept"])


class Reversibility(unittest.TestCase):
    def test_every_v1_field_survives(self):
        v1 = {"x": {"discord_id": BOT, "stand_status": f"stand {BOT}",
                    "verification": "corpus-observed", "verified_at": "2026-08-02",
                    "source": "id-map", "observed_at": "2026-08-26", "evidence": "e"}}
        entry = migrate(v1)[0]["x"]
        for k, v in v1["x"].items():
            self.assertEqual(entry[k], v, f"{k} was dropped or rewritten")

    def test_cli_refuses_to_write_over_its_input(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "roster.json"
            src.write_text(json.dumps({"x": {"discord_id": BOT}}))
            before = src.read_bytes()
            import sys
            argv = sys.argv
            sys.argv = ["m", "--roster", str(src), "--out", str(src)]
            try:
                self.assertEqual(mig.main(), 2)
            finally:
                sys.argv = argv
            self.assertEqual(src.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
