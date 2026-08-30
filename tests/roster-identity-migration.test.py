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
import os
import pathlib
import shutil
import subprocess
import sys
import contextlib
import io
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



class TheCommandLineActuallyRuns(unittest.TestCase):
    """The CLI, its table and its refusals — run IN PROCESS so coverage sees it.

    A subprocess is not traced by the gate, so the same assertions via
    subprocess would pass while leaving these lines measured as uncovered.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-")
        self._argv = sys.argv

    def tearDown(self):
        sys.argv = self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self, d):
        p = Path(self.tmp) / "roster.json"
        p.write_text(json.dumps(d))
        return p

    def _main(self, *argv):
        sys.argv = ["m", *argv]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mig.main()
        return rc, buf.getvalue()

    def test_it_migrates_and_prints_the_before_after_table(self):
        src = self._roster({"qingyun-wu": {"discord_id": BOT}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"qingyun-wu": {
            "discord": HUMAN, "bots": [BOT]}}}))
        out = Path(self.tmp) / "v2.json"
        rc, txt = self._main("--roster", str(src), "--triage-config", str(cfg),
                             "--out", str(out), "--table")
        self.assertEqual(rc, 0)
        self.assertIn("-> human_discord_id", txt)
        got = json.loads(out.read_text())["qingyun-wu"]
        self.assertEqual(got["human_discord_id"], HUMAN)
        self.assertEqual(got["stand_discord_id"], BOT)

    def test_two_ids_claiming_the_human_slot_is_a_conflict_not_a_pick(self):
        src = self._roster({"z": {"discord_human_id": HUMAN,
                                  "human_discord_id": "1358841611580080168"}})
        out = Path(self.tmp) / "v2.json"
        rc, _ = self._main("--roster", str(src), "--out", str(out))
        self.assertEqual(rc, 5, "a conflict leaves an unresolved id: a coverage gap")
        rec = json.loads(out.read_text())["z"]
        self.assertIsNone(rec["human_discord_id"],
                          "a conflict must not resolve to one of them")
        reasons = [u["reason"] for u in rec["unresolved_discord_ids"]]
        self.assertTrue(any("two ids claim the human slot" in r for r in reasons), reasons)

    def test_peers_and_owner_config_are_read_when_supplied(self):
        # Both are optional inputs, so nothing else exercises their read path —
        # and they are what classify a peer bot id and the owner's own id.
        src = self._roster({"p": {"discord_id": BOT}, "o": {"discord_id": HUMAN}})
        peers = Path(self.tmp) / "peers.json"
        peers.write_text(json.dumps({"pro": BOT}))
        dcfg = Path(self.tmp) / "discord-config.json"
        dcfg.write_text(json.dumps({"owner": HUMAN}))
        out = Path(self.tmp) / "v2.json"
        rc, _ = self._main("--roster", str(src), "--peers", str(peers),
                           "--discord-config", str(dcfg), "--out", str(out))
        self.assertEqual(rc, 0)
        d = json.loads(out.read_text())
        self.assertEqual(d["p"]["stand_discord_id"], BOT, "peers.json states it is a bot")
        self.assertEqual(d["o"]["human_discord_id"], HUMAN, "discord-config states the owner")


    def test_a_triage_only_login_is_not_dropped(self):
        # The roster is keyed by person, pr-triage by GitHub login. Iterating
        # only roster rows silently loses everyone pr-triage alone knows.
        src = self._roster({})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"john-the-dev": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--triage-config", str(cfg), "--out", str(out))
        got = json.loads(out.read_text())
        self.assertIn("john-the-dev", got)
        self.assertEqual(got["john-the-dev"]["human_discord_id"], HUMAN)

    def test_a_roster_row_is_joined_on_its_declared_github_login(self):
        # key `rui`, field `github: john-the-dev` — joining on the key alone
        # leaves the row unenriched while the evidence sits one field away.
        src = self._roster({"rui": {"github": "john-the-dev"}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"john-the-dev": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--triage-config", str(cfg), "--out", str(out))
        got = json.loads(out.read_text())
        self.assertEqual(got["rui"]["human_discord_id"], HUMAN)
        self.assertNotIn("john-the-dev", got, "the alias must not become a second person")

    def test_a_prefix_id_is_not_claimed_by_a_longer_ids_citation(self):
        # 17 digits is a substring of 18: substring matching published the
        # wrong referent into the slot this schema exists to protect.
        src = self._roster({"x": {"discord_id": "12345678901234567",
                                  "stand_status": "stand id 123456789012345678"}})
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out))
        rec = json.loads(out.read_text())["x"]
        # The 18-digit id IS the stand — stand_status cites it. The 17-digit one
        # must stay unresolved rather than inheriting that citation.
        self.assertEqual(rec["stand_discord_id"], "123456789012345678")
        self.assertEqual([u["id"] for u in rec["unresolved_discord_ids"]],
                         ["12345678901234567"])

    def test_typed_values_that_are_not_snowflakes_are_refused(self):
        src = self._roster({"y": {}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"y": {"discord": "not-a-snowflake",
                                                    "bots": "12"}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--triage-config", str(cfg), "--out", str(out))
        rec = json.loads(out.read_text())["y"]
        self.assertIsNone(rec["human_discord_id"])
        self.assertIsNone(rec["stand_discord_id"])
        # CHANGED (#3537): neither value contains an id, and this field is
        # documented as ids only — no invented ones. rc is still 5.
        self.assertEqual(rec["unresolved_discord_ids"], [])

    def test_a_supplied_but_missing_source_is_an_error_not_a_silent_skip(self):
        src = self._roster({"x": {}})
        with self.assertRaises(SystemExit) as cm:
            self._main("--roster", str(src), "--out", str(Path(self.tmp) / "v2.json"),
                       "--triage-config", str(Path(self.tmp) / "absent.json"))
        self.assertEqual(cm.exception.code, 2)

    def test_an_input_v1_schema_does_not_survive_into_the_output(self):
        src = self._roster({"_schema": {"version": "reviewer-identity/1"}, "x": {}})
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out))
        self.assertEqual(json.loads(out.read_text())["_schema"]["version"],
                         mig.ri.SCHEMA_VERSION)


    def test_typed_evidence_alone_is_enough_to_classify(self):
        # schema.md names stand_status as evidence, so an id living only there
        # must be discovered — not merely matched if some discord* field repeats it.
        src = self._roster({"y": {"stand_status": "stand id 1504316176686120980"}})
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out))
        self.assertEqual(json.loads(out.read_text())["y"]["stand_discord_id"],
                         "1504316176686120980")

    def test_a_declared_github_login_never_falls_back_to_the_local_key(self):
        # people.rui may be a DIFFERENT person; falling back crosses axes and
        # then records people.rui as the provenance of a john-the-dev fact.
        src = self._roster({"rui": {"github": "john-the-dev"}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"rui": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        self.assertIsNone(json.loads(out.read_text())["rui"]["human_discord_id"])

    def test_a_bots_object_is_refused_rather_than_iterated_as_keys(self):
        src = self._roster({"x": {}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"x": {"bots": {HUMAN: "human"}}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        rec = json.loads(out.read_text())["x"]
        self.assertIsNone(rec["stand_discord_id"], "a dict must not contribute its keys")


    def test_an_alias_collision_is_rejected_with_its_evidence_intact(self):
        # roster `rui` declares github john-the-dev while triage also has a
        # `people.rui`. Dropping that row loses a real identity silently.
        src = self._roster({"rui": {"github": "john-the-dev"}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"rui": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2.json"
        self.assertEqual(self._main("--roster", str(src), "--out", str(out),
                                    "--triage-config", str(cfg))[0], 5)
        rec = json.loads(out.read_text())["rui"]
        self.assertIsNone(rec["human_discord_id"], "the two axes must not merge")
        hits = [u for u in rec["unresolved_discord_ids"] if u["id"] == HUMAN]
        self.assertTrue(hits, "the colliding identity vanished instead of being rejected")
        self.assertIn("collides with roster key", hits[0]["reason"])
        self.assertIn("john-the-dev", hits[0]["reason"])

    def test_resolved_and_unresolved_sets_are_disjoint(self):
        # A numeric triage `discord` equal to a valid roster id put the same
        # canonical id in both outputs.
        src = self._roster({"z": {"discord_human_id": HUMAN}})
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"people": {"z": {"discord": int(HUMAN)}}}))
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        rec = json.loads(out.read_text())["z"]
        resolved = {rec["human_discord_id"], rec["stand_discord_id"]} - {None}
        unresolved = {u["id"] for u in rec["unresolved_discord_ids"]}
        self.assertFalse(resolved & unresolved, f"{resolved} also in {unresolved}")
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_a_present_bots_value_is_validated_by_type_not_truthiness(self):
        # `{}` and `""` are falsy, so a truthiness guard let them through and
        # the run reported success on a shape the schema forbids.
        for bad, label in (({}, "empty object"), ("", "empty string")):
            src = self._roster({"x": {}})
            cfg = Path(self.tmp) / f"cfg-{label.replace(' ', '')}.json"
            cfg.write_text(json.dumps({"people": {"x": {"discord": HUMAN, "bots": bad}}}))
            out = Path(self.tmp) / f"v2-{label.replace(' ', '')}.json"
            self.assertEqual(
                self._main("--roster", str(src), "--out", str(out),
                           "--triage-config", str(cfg))[0], 5,
                f"a {label} bypassed the documented list requirement")

    def test_typed_evidence_states_the_referent_for_ids_nested_under_it(self):
        # schema.md says `secondary_agent` itself names the referent; testing
        # only the leaf key ("id") discards exactly that evidence.
        src = self._roster({"y": {"secondary_agent": {"id": "1504316176686120980"}}})
        out = Path(self.tmp) / "v2.json"
        self._main("--roster", str(src), "--out", str(out))
        rec = json.loads(out.read_text())["y"]
        self.assertEqual(rec["stand_discord_id"], "1504316176686120980")
        self.assertEqual(rec["unresolved_discord_ids"], [])

    def test_malformed_evidence_that_opposes_the_resolved_slot_stays_unresolved(self):
        # The valid and malformed spellings of the same disagreement must agree:
        # a malformed one used to be demoted to a note and publish the opposite slot.
        SID = "1504316176686120980"
        for people, label in (({"x": {"discord": SID}}, "valid human string"),
                              ({"x": {"discord": int(SID)}}, "numeric human"),):
            src = self._roster({"x": {"stand_discord_id": SID}})
            cfg = Path(self.tmp) / f"c-{label.replace(' ', '')}.json"
            cfg.write_text(json.dumps({"people": people}))
            out = Path(self.tmp) / f"o-{label.replace(' ', '')}.json"
            rc = self._main("--roster", str(src), "--out", str(out),
                            "--triage-config", str(cfg))[0]
            rec = json.loads(out.read_text())["x"]
            self.assertEqual(rc, 5, f"{label} was published as success")
            self.assertIsNone(rec["stand_discord_id"], label)
            self.assertEqual([u["id"] for u in rec["unresolved_discord_ids"]], [SID], label)

    def test_a_malformed_bots_value_opposing_a_human_stays_unresolved(self):
        SID = "1504316176686120980"
        for bots, label in (([SID], "valid list"), (SID, "bare string")):
            src = self._roster({"x": {"discord_human_id": SID}})
            cfg = Path(self.tmp) / f"cb-{label.replace(' ', '')}.json"
            cfg.write_text(json.dumps({"people": {"x": {"bots": bots}}}))
            out = Path(self.tmp) / f"ob-{label.replace(' ', '')}.json"
            rc = self._main("--roster", str(src), "--out", str(out),
                            "--triage-config", str(cfg))[0]
            rec = json.loads(out.read_text())["x"]
            self.assertEqual(rc, 5, f"{label} was published as success")
            self.assertIsNone(rec["human_discord_id"], label)

    def test_an_agreeing_malformed_observation_is_still_only_a_note(self):
        # The negative control: without it, treating every malformed row as
        # disagreement would pass the two cases above and block everything.
        src = self._roster({"x": {"discord_human_id": HUMAN}})
        cfg = Path(self.tmp) / "cn.json"
        cfg.write_text(json.dumps({"people": {"x": {"discord": int(HUMAN)}}}))
        out = Path(self.tmp) / "on.json"
        self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        rec = json.loads(out.read_text())["x"]
        self.assertEqual(rec["human_discord_id"], HUMAN)
        self.assertEqual(rec["unresolved_discord_ids"], [])

    def test_a_case_only_github_alias_does_not_split_one_reviewer(self):
        # GitHub logins are case-insensitive, so `John-The-Dev` and
        # `john-the-dev` are one login; matching case-sensitively made two rows.
        src = self._roster({"rui": {"github": "John-The-Dev",
                                    "stand_discord_id": "1504316176686120980"}})
        cfg = Path(self.tmp) / "cc.json"
        cfg.write_text(json.dumps({"people": {"john-the-dev": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "oc.json"
        self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        doc = json.loads(out.read_text())
        self.assertEqual(sorted(k for k in doc if k != "_schema"), ["rui"])
        self.assertEqual(doc["rui"]["human_discord_id"], HUMAN)
        self.assertEqual(doc["rui"]["stand_discord_id"], "1504316176686120980")

    def test_a_hardlinked_destination_is_refused_and_the_input_survives(self):
        # A hardlink resolves to a different NAME and the same inode, so a
        # pathname check alone overwrites v1 and destroys the rollback.
        src = self._roster({"x": {}})
        link = Path(self.tmp) / "hard.json"
        os.link(src, link)
        rc = self._main("--roster", str(src), "--out", str(link))[0]
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(src.read_text()), {"x": {}})

    def test_a_typed_ancestor_states_the_referent_at_any_depth(self):
        SID = "1504316176686120980"
        for doc, label in (({"y": {"stand_status": {"id": SID}}}, "direct"),
                           ({"y": {"wrapper": {"stand_status": {"id": SID}}}}, "nested")):
            src = self._roster(doc)
            out = Path(self.tmp) / f"v2-{label}.json"
            self._main("--roster", str(src), "--out", str(out))
            self.assertEqual(json.loads(out.read_text())["y"]["stand_discord_id"], SID,
                             f"{label} nesting changed the classification")

    def test_container_shaped_opposing_evidence_attaches_to_the_id(self):
        # str(container) attached the disagreement to a repr no reader matches,
        # so the id it opposed kept its slot.
        SID = "1504316176686120980"
        src = self._roster({"x": {"stand_discord_id": SID}})
        cfg = Path(self.tmp) / "cc2.json"
        cfg.write_text(json.dumps({"people": {"x": {"discord": [SID]}}}))
        out = Path(self.tmp) / "v2c.json"
        rc = self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))[0]
        rec = json.loads(out.read_text())["x"]
        self.assertEqual(rc, 5)
        self.assertIsNone(rec["stand_discord_id"])
        self.assertEqual([u["id"] for u in rec["unresolved_discord_ids"]], [SID])

    def test_a_case_only_local_key_collision_is_still_a_collision(self):
        src = self._roster({"Rui": {"github": "john-the-dev",
                                    "stand_discord_id": "1504316176686120980"}})
        cfg = Path(self.tmp) / "cc3.json"
        cfg.write_text(json.dumps({"people": {"rui": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2r.json"
        rc = self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))[0]
        rec = json.loads(out.read_text())["Rui"]
        self.assertEqual(rc, 5, "a case-only collision was published as success")
        self.assertIn(HUMAN, [u["id"] for u in rec["unresolved_discord_ids"]])

    def test_conflicting_case_variant_triage_keys_are_refused(self):
        src = self._roster({"rui": {"github": "John-The-Dev"}})
        cfg = Path(self.tmp) / "cc4.json"
        cfg.write_text(json.dumps({"people": {"John-The-Dev": {"discord": HUMAN},
                                              "john-the-dev": {"discord": "1504316176686120980"}}}))
        out = Path(self.tmp) / "v2d.json"
        rc, _ = self._main("--roster", str(src), "--out", str(out), "--triage-config", str(cfg))
        self.assertEqual(rc, 2, "one of two conflicting spellings disappeared silently")

    def test_the_audit_row_reads_the_matched_triage_source(self):
        # Reading triage_people[key] on an aliased row printed "triage human = -"
        # while filling after_human from the same record.
        src = self._roster({"rui": {"github": "john-the-dev"}})
        cfg = Path(self.tmp) / "cc5.json"
        cfg.write_text(json.dumps({"people": {"john-the-dev": {"discord": HUMAN}}}))
        out = Path(self.tmp) / "v2t.json"
        _rc, stdout = self._main("--roster", str(src), "--out", str(out),
                                 "--triage-config", str(cfg), "--table")
        # The BEFORE column specifically: asserting the id appears anywhere
        # passes on after_human too, which is filled either way.
        row = next(l for l in stdout.splitlines() if l.startswith("rui"))
        before = row.split()[1:3]
        self.assertIn(HUMAN, before,
                      f"the 'triage human' column lost the matched source: {row!r}")

    def test_a_prelinked_temp_path_cannot_destroy_the_input(self):
        # A deterministic <dest>.tmp can already be a hardlink to the roster,
        # and write_text follows it before the destination guard ever runs.
        src = self._roster({"x": {}})
        dest = Path(self.tmp) / "out_pl.json"
        os.link(src, str(dest) + ".tmp")
        rc = self._main("--roster", str(src), "--out", str(dest))[0]
        self.assertEqual(json.loads(src.read_text()), {"x": {}},
                         "the migration overwrote its own input through the temp")

    def test_the_migration_is_semantically_idempotent(self):
        # `other_stand_discord_ids` begins with "other_", so the secondary
        # Stand was not typed evidence on a rerun and got demoted.
        SEC = "1529720369668292629"
        src = self._roster({"y": {"stand_discord_id": "1504316176686120980",
                                  "other_stand_discord_ids": [{"id": SEC, "basis": "x"}]}})
        first = Path(self.tmp) / "v2-1.json"
        self._main("--roster", str(src), "--out", str(first))
        second = Path(self.tmp) / "v2-2.json"
        rc = self._main("--roster", str(first), "--out", str(second))[0]
        rec = json.loads(second.read_text())["y"]
        self.assertEqual([o["id"] for o in rec["other_stand_discord_ids"]], [SEC],
                         "a rerun demoted the secondary Stand")
        self.assertEqual(rec["unresolved_discord_ids"], [])
        self.assertEqual(rc, 0)

    def test_a_coverage_gap_is_reported_rather_than_read_as_success(self):
        # rc 5 says the file WAS written and somebody in it is unaddressable.
        # rc 0 would let a caller promote a map that reaches nobody.
        src = self._roster({"nobody": {}})
        out = Path(self.tmp) / "v2.json"
        rc, _ = self._main("--roster", str(src), "--out", str(out))
        self.assertEqual(rc, 5)
        self.assertTrue(out.is_file(), "rc 5 still writes the file")


    def test_an_id_inside_a_list_is_still_found(self):
        src = self._roster({"w": {"discord_ids": [BOT]}})
        out = Path(self.tmp) / "v2.json"
        rc, _ = self._main("--roster", str(src), "--out", str(out))
        self.assertEqual(rc, 5, "an id with no stated referent is a coverage gap")
        self.assertIn(BOT, json.dumps(json.loads(out.read_text())["w"]),
                      "a list-valued id must not be dropped")


class AnAxisCollisionBlocksWithoutAnyId(unittest.TestCase):
    """The reviewer's exact production-CLI fixture (#3537).

    The collision was reported only from inside a loop over the ids extracted
    from the colliding row, so an EMPTY row yielded no ids, no record, and rc 0
    — the gate keyed on an id being present rather than on the axis clash.
    """

    ROSTER = {"rui": {"github": "john-the-dev",
                      "stand_status": f"stand id {BOT}"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collide-")
        self._argv = sys.argv

    def tearDown(self):
        sys.argv = self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, triage):
        d = Path(self.tmp)
        (d / "roster.json").write_text(json.dumps(self.ROSTER))
        (d / "cfg.json").write_text(json.dumps(triage))
        out = d / "v2.json"
        sys.argv = ["m", "--roster", str(d / "roster.json"),
                    "--triage-config", str(d / "cfg.json"), "--out", str(out)]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = mig.main()
        doc = json.loads(out.read_text()) if out.exists() else {}
        return rc, err.getvalue(), doc

    def test_an_empty_colliding_row_still_blocks(self):
        # THE case: `people.rui = {}` collides with roster key `rui` whose
        # declared github is `john-the-dev`, and carries no id at all.
        rc, err, doc = self._run({"people": {"rui": {}}})
        self.assertEqual(rc, 5, "an id-less collision must not exit 0")
        self.assertIn("COLLISION rui", err)
        self.assertEqual(sorted(k for k in doc if not k.startswith("_")), ["rui"])

    def test_the_same_collision_carrying_an_id_still_blocks(self):
        # Was already 5 before the fix; kept so a regression cannot pass by
        # breaking the id path while the id-less one is watched.
        rc, err, doc = self._run({"people": {"rui": {"discord": HUMAN}}})
        self.assertEqual(rc, 5)
        unres = [u for v in doc.values() if isinstance(v, dict)
                 for u in (v.get("unresolved_discord_ids") or [])]
        self.assertIn(HUMAN, [u["id"] for u in unres])

    def test_a_non_colliding_extra_row_is_not_reported_as_a_collision(self):
        # Negative control. Without it, flagging every triage key would pass
        # the two cases above and be indistinguishable from the fix.
        rc, err, doc = self._run({"people": {"other-reviewer": {}}})
        self.assertEqual(rc, 5, "still 5, but for a GAP")
        self.assertNotIn("COLLISION", err)
        self.assertIn("GAP other-reviewer", err)
        self.assertEqual(sorted(k for k in doc if not k.startswith("_")),
                         ["other-reviewer", "rui"])

    def test_no_triage_row_at_all_is_clean(self):
        # Second negative control: the collision must need a real clash.
        rc, err, _ = self._run({"people": {}})
        self.assertNotIn("COLLISION", err)


class AFieldNameStatesAWordNotASubstring(unittest.TestCase):
    """`_verdict_from_field` matched substrings, so a name could carry a
    referent it never claimed (#3537). Reviewer's production-CLI controls."""

    def _verdict(self, field):
        return mig._verdict_from_field(field)[0]

    def test_words_that_merely_contain_the_token_state_nothing(self):
        for field in ("understanding_discord_id", "agentless_discord_id",
                      "inhuman_discord_id", "standard_id", "humanoid_id",
                      "management_id"):
            self.assertIsNone(self._verdict(field), field)

    def test_the_real_typed_fields_still_state_their_referent(self):
        # The positive control: without it, returning None always would pass
        # the negative cases and silently disable every classification.
        for field in ("discord_human_id", "human_discord_id"):
            self.assertEqual(self._verdict(field), mig.HUMAN, field)
        for field in ("stand_discord_id", "stand_status", "secondary_agent",
                      "other_stand_discord_ids", "wrapper.stand_status.id"):
            self.assertEqual(self._verdict(field), mig.STAND, field)

    def test_camel_case_is_split_too(self):
        self.assertEqual(self._verdict("standDiscordId"), mig.STAND)
        self.assertEqual(self._verdict("humanDiscordId"), mig.HUMAN)
        self.assertIsNone(self._verdict("understandingId"))

    def test_an_unstated_field_leaves_the_id_unresolved_not_guessed(self):
        # End to end: the whole point is that no slot gets filled from a name
        # that never named a referent.
        (human, stand, _others, unresolved, _basis, _coll,
         _shapes) = mig.classify(
            "x", {"understanding_discord_id": HUMAN}, {}, {}, "")
        self.assertIsNone(human)
        self.assertIsNone(stand)
        self.assertEqual([u["id"] for u in unresolved], [HUMAN])


class MemberOrderIsNotIdentityEvidence(unittest.TestCase):
    """Re-running a v2 record must not let JSON member order pick the primary.

    `_collect_ids` walks in traversal order and the first Stand claim became
    primary, so writing the schema's own two fields in the other order swapped
    the agents while still returning 0 (#3537).
    """

    SECOND = "1529720369668292629"

    def _primary_and_others(self, doc):
        out, _ = migrate(doc)
        e = out["x"]
        return ri.stand_discord_id(e), [o["id"] if isinstance(o, dict) else o
                                        for o in e.get(ri.OTHER_STANDS_FIELD) or []]

    def test_secondary_first_still_yields_the_declared_primary(self):
        # The reviewer's exact rerun: only the member order differs.
        primary, others = self._primary_and_others(
            {"x": {ri.OTHER_STANDS_FIELD: [self.SECOND], ri.STAND_FIELD: BOT}})
        self.assertEqual(primary, BOT)
        self.assertEqual(others, [self.SECOND])

    def test_primary_first_is_unchanged(self):
        # Positive control: passes at both revisions, so a regression that
        # simply reversed the order could not masquerade as the fix.
        primary, others = self._primary_and_others(
            {"x": {ri.STAND_FIELD: BOT, ri.OTHER_STANDS_FIELD: [self.SECOND]}})
        self.assertEqual(primary, BOT)
        self.assertEqual(others, [self.SECOND])

    def test_the_reader_contract_agrees_in_both_orders(self):
        # roster_identity.stand_discord_ids promises "primary stand first".
        for doc in ({"x": {ri.STAND_FIELD: BOT, ri.OTHER_STANDS_FIELD: [self.SECOND]}},
                    {"x": {ri.OTHER_STANDS_FIELD: [self.SECOND], ri.STAND_FIELD: BOT}}):
            out, _ = migrate(doc)
            self.assertEqual(ri.stand_discord_ids(out["x"]), [BOT, self.SECOND])

    def test_a_v1_doc_declaring_neither_field_keeps_traversal_order(self):
        # Negative control: with no schema statement there is nothing to rank
        # by, and the ranking must not silently reorder those either.
        doc = {"x": {"discord_id": BOT, "stand_status": f"stand id {BOT}",
                     "secondary_agent": {"discord_id": self.SECOND}}}
        primary, others = self._primary_and_others(doc)
        self.assertEqual(primary, BOT)
        self.assertEqual(others, [self.SECOND])


class AFieldNamingBothReferentsStatesNeither(unittest.TestCase):
    """`human` was checked first, so a name stating BOTH silently became HUMAN.

    That is a precedence no source documents. Two stated referents are a
    disagreement, and the migration already has a disagreement path.
    """

    def _entry(self, doc):
        out, _ = migrate(doc)
        return out["x"]

    def test_a_flat_both_referent_name_is_unresolved(self):
        e = self._entry({"x": {"human_agent_discord_id": BOT}})
        self.assertIsNone(ri.human_discord_id(e))
        self.assertIsNone(ri.stand_discord_id(e))
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(e)], [BOT])

    def test_a_nested_both_referent_path_is_unresolved(self):
        # The path is what states both — `human_profile` and `secondary_agent`
        # are different segments, so a leaf-only check would miss it.
        e = self._entry({"x": {"human_profile": {"secondary_agent": {"id": BOT}}}})
        self.assertIsNone(ri.human_discord_id(e))
        self.assertIsNone(ri.stand_discord_id(e))
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(e)], [BOT])

    def test_single_referent_names_are_untouched(self):
        # Positive control: passes at both revisions, so a fix that resolved
        # nothing at all could not satisfy the two negatives and look correct.
        self.assertEqual(
            ri.human_discord_id(self._entry({"x": {"discord_human_id": HUMAN}})), HUMAN)
        self.assertEqual(
            ri.stand_discord_id(self._entry({"x": {"stand_discord_id": BOT}})), BOT)

    def test_the_id_is_still_collected_rather_than_vanishing(self):
        # The trap: a both-referent segment that stops counting as typed
        # drops the id entirely instead of reporting it unresolved.
        e = self._entry({"x": {"human_agent_discord_id": BOT}})
        self.assertTrue(ri.unresolved_discord_ids(e), "id must survive as unresolved")


class EveryBadCallSiteIsExercised(unittest.TestCase):
    """`_bad()` takes a required `shapes` list. A caller that forgets it raises
    TypeError at RUNTIME, on the malformed input the argument exists to report —
    so the crash lands exactly where the diagnostic was promised. One case per
    call site, because a missed caller is invisible until its branch is taken."""

    ROSTER = {"x": {"stand_status": f"stand id {BOT}"}}

    def _run(self, triage):
        out, _ = mig.migrate(self.ROSTER, triage, {}, "", "test.json")
        return out["x"]

    def test_triage_discord_that_is_not_a_snowflake(self):
        self._run({"x": {"discord": "not-a-snowflake"}})

    def test_triage_bots_that_is_not_a_list(self):
        self._run({"x": {"bots": {"a": 1}}})

    def test_a_bots_entry_that_is_not_a_snowflake(self):
        # The one that was missed: it only fires when a LIST is supplied and an
        # ENTRY inside it is malformed, which no other case reaches.
        self._run({"x": {"bots": ["not-a-snowflake"]}})

    def test_the_helper_still_requires_the_list(self):
        # Negative control: if `shapes` were made optional the three cases above
        # would pass with the bug reintroduced, so pin the signature itself.
        import inspect
        sig = inspect.signature(mig._bad)
        self.assertIs(sig.parameters["shapes"].default, inspect.Parameter.empty,
                      "shapes must stay required, or a missed caller goes silent")


class ADeclaredIdSlotFailsClosedOnEveryPresentValue(unittest.TestCase):
    """A singular typed slot discarded a value it could read no id from, so the
    CLI returned 0 having dropped explicitly human-typed evidence (#3537).

    Only a NON-STRING scalar reached the shape branch, so `12` exited 5 while
    the string and empty-container cases beside it exited 0. Run through the
    CLI, because the exit status is what a promoting caller reads.
    """

    STAND = "1500000000000000001"
    SECOND = "1529720369668292629"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="slot-")
        self._argv = sys.argv

    def tearDown(self):
        sys.argv = self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli(self, entry):
        src = Path(self.tmp) / "roster.json"
        src.write_text(json.dumps({"x": entry}))
        out = Path(self.tmp) / "v2.json"
        sys.argv = ["m", "--roster", str(src), "--out", str(out)]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = mig.main()
        return rc, err.getvalue(), json.loads(out.read_text())["x"]

    def _typed(self, value):
        return self._cli({"discord_human_id": value,
                          "stand_discord_id": self.STAND})

    def test_a_string_holding_no_id_is_refused(self):
        rc, err, rec = self._typed("not-a-snowflake")
        self.assertEqual(rc, 5, "human evidence was discarded, not absent")
        self.assertIn("SHAPE x", err)
        self.assertIsNone(rec["human_discord_id"])

    def test_an_empty_object_is_refused(self):
        rc, err, _ = self._typed({})
        self.assertEqual(rc, 5)
        self.assertIn("SHAPE x", err)

    def test_an_empty_list_is_refused(self):
        rc, err, _ = self._typed([])
        self.assertEqual(rc, 5)
        self.assertIn("SHAPE x", err)

    def test_a_number_is_still_refused(self):
        # The reviewer's control: it passed BEFORE the fix, so a regression
        # widening only the string path would satisfy the three cases above.
        rc, err, _ = self._typed(12)
        self.assertEqual(rc, 5)
        self.assertIn("SHAPE x", err)

    def test_a_scalar_in_a_declared_slot_is_recorded_once(self):
        # Both branches can reach a non-string here. Assert on the AUDIT ROW:
        # stderr prints one line per person, so a stderr count cannot fail.
        _out, rows = mig.migrate({"x": {"discord_human_id": 12}}, {}, {}, "",
                                 "test.json")
        self.assertEqual(len(rows[0]["after_shape_failure"]), 1)

    def test_a_readable_id_still_fills_the_slot(self):
        # The positive control. Refusing everything would pass all four above.
        rc, _err, rec = self._typed("1500000000000000009")
        self.assertEqual(rc, 0)
        self.assertEqual(rec["human_discord_id"], "1500000000000000009")

    def test_an_absent_slot_is_absent_not_malformed(self):
        # `roster_identity`'s readers coerce None and blank to None; flagging
        # either here would make the migration disagree with every consumer.
        for value in (None, ""):
            rc, err, _ = self._cli({"human_discord_id": value,
                                    "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, repr(value))
            self.assertNotIn("SHAPE", err)

    def test_a_free_form_field_naming_a_referent_is_not_an_id_slot(self):
        # `stand_status` states the referent and holds prose. Keying the check
        # on "typed" rather than on the name's last word refuses this.
        rc, err, _ = self._cli({"stand_discord_id": self.STAND,
                                "stand_status": "active"})
        self.assertEqual(rc, 0)
        self.assertNotIn("SHAPE", err)

    def test_a_schema_owned_empty_collection_survives_re_migration(self):
        # The v2 writer emits these; refusing them makes the migration refuse
        # its own output, which is how the first attempt at this broke.
        rc, err, _ = self._cli({"stand_discord_id": self.STAND,
                                ri.OTHER_STANDS_FIELD: [],
                                ri.UNRESOLVED_FIELD: []})
        self.assertEqual(rc, 0)
        self.assertNotIn("SHAPE", err)

    def test_the_basis_map_is_keyed_by_slot_names_and_holds_prose(self):
        # `id_basis.stand_discord_id` names the slot being EXPLAINED; its value
        # is a reason, never an id. Checking it flags every migrated document.
        rc, err, _ = self._cli({
            "stand_discord_id": self.STAND,
            ri.BASIS_FIELD: {"stand_discord_id": ["cited in `stand_status`"]}})
        self.assertEqual(rc, 0)
        self.assertNotIn("SHAPE", err)

    # `schema.md` documents these as ordinary string identifiers, not snowflakes.
    FOREIGN = {"entity_id": "person-rui", "user_id": "octo-dev",
               "room_id": "room-7", "provider_room_id": "!abc:ag2.space",
               "provider_user_id": "user-42"}

    def test_a_foreign_id_field_is_not_a_discord_slot(self):
        # Reviewer, 2026-08-30: the last word alone made every `*_id` a
        # snowflake slot, so a valid roster returned 5 and could not promote.
        for field, value in self.FOREIGN.items():
            rc, err, _ = self._cli({field: value,
                                    "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, f"{field}={value!r}: {err}")
            self.assertNotIn("SHAPE", err)

    def test_a_foreign_id_beside_a_malformed_discord_slot_still_refuses(self):
        # The pairing the reviewer asked for: scoping the discriminator must
        # not be achieved by disabling it. One row, both fields.
        entry = dict(self.FOREIGN)
        entry["stand_discord_id"] = self.STAND
        entry["discord_human_id"] = "not-a-snowflake"
        rc, err, rec = self._cli(entry)
        self.assertEqual(rc, 5, err)
        self.assertIn("SHAPE x", err)
        self.assertIsNone(rec["human_discord_id"])
        for field, value in self.FOREIGN.items():
            self.assertEqual(rec[field], value, f"{field} was rewritten")

    def test_a_foreign_id_under_a_referent_ancestor_is_not_a_discord_slot(self):
        # Reviewer, 2026-08-30: `_typed_path` matches ANY ancestor, so nesting
        # a provider-native id under `human` made it a snowflake slot.
        for parent in ("human", "secondary_agent"):
            for field, value in self.FOREIGN.items():
                rc, err, rec = self._cli({parent: {field: value},
                                          "stand_discord_id": self.STAND})
                self.assertEqual(rc, 0, f"{parent}.{field}={value!r}: {err}")
                self.assertNotIn("SHAPE", err)
                self.assertEqual(rec[parent][field], value)

    def test_a_bare_id_under_a_referent_ancestor_is_still_a_discord_slot(self):
        # Scoping the leaf must not delete the ancestor evidence: this key
        # has no referent of its own and needs its parent.
        rc, _err, rec = self._cli({"secondary_agent": {"id": self.SECOND},
                                   "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0)
        self.assertIn(self.SECOND,
                      [o["id"] for o in rec[ri.OTHER_STANDS_FIELD]])
        rc, err, _ = self._cli({"secondary_agent": {"id": "not-a-snowflake"},
                                "stand_discord_id": self.STAND})
        self.assertEqual(rc, 5, err)
        self.assertIn("SHAPE x", err)

    def test_a_snowflake_shaped_foreign_id_is_not_mined_into_a_slot(self):
        # Reviewer, 2026-08-30: the leaf rule reached VALIDATION only, so
        # discovery mined a provider id that merely looked like a snowflake.
        for parent, slot in (("human", "human_discord_id"),
                             ("secondary_agent", ri.OTHER_STANDS_FIELD)):
            rc, err, rec = self._cli({parent: {"provider_user_id": HUMAN},
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            got = rec[slot]
            got = [o["id"] for o in got] if isinstance(got, list) else got
            self.assertNotIn(HUMAN, got if isinstance(got, list) else [got],
                             f"{parent}.provider_user_id was read as Discord")
            self.assertEqual(rec[parent]["provider_user_id"], HUMAN)

    def test_the_collector_still_reads_a_referent_leaf_and_a_bare_id(self):
        # Positive controls: skipping every `*_id` leaf would pass the test
        # above and stop the migration classifying anything at all.
        rc, _e, rec = self._cli({"secondary_agent": {"id": self.SECOND}})
        self.assertEqual(rc, 0)
        self.assertIn(self.SECOND, ri.stand_discord_ids(rec))
        rc, _e, rec = self._cli({"stand_status": f"stand id {self.SECOND}"})
        self.assertEqual(rc, 0)
        self.assertEqual(ri.stand_discord_id(rec), self.SECOND)

    def test_a_foreign_leaf_cannot_cite_an_id_a_real_slot_already_holds(self):
        # Same digits in both: without the citation skip the provider field
        # adds a HUMAN claim, they disagree, and a real Stand goes unresolved.
        rc, err, rec = self._cli({"stand_discord_id": BOT,
                                  "human": {"provider_user_id": BOT}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(ri.stand_discord_id(rec), BOT)
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(rec)], [])

    def test_another_providers_key_is_not_a_discord_slot(self):
        # Reviewer, 2026-08-30: naming the REFERENT is not naming DISCORD.
        # `telegram_human_id` routed a Discord ping to a Telegram number.
        for field in ("telegram_human_id", "slack_human_id",
                      "matrix_stand_id", "github_human_id"):
            rc, err, rec = self._cli({field: HUMAN,
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            self.assertIsNone(rec["human_discord_id"], field)

    def test_a_sibling_provider_field_supplies_the_discord_evidence(self):
        # The inverse, and the documented shape: `{provider, user_id}`. The
        # leaf alone says nothing, so dropping it loses a reachable person.
        rc, err, rec = self._cli({
            "human": {"identities": [{"provider": "discord",
                                      "user_id": HUMAN}]},
            "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_a_sibling_naming_another_provider_is_still_excluded(self):
        # Paired negative: reading the sibling must not mean trusting it.
        rc, err, rec = self._cli({
            "human": {"identities": [{"provider": "matrix",
                                      "user_id": HUMAN}]},
            "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])

    def test_a_referent_key_naming_an_unknown_provider_fails_closed(self):
        # CHANGED (#3537): asserted the opposite, on a fixture I invented.
        # Measured: the live roster holds NO unqualified id key.
        for field in ("teams_human_id", "webex_human_id", "other_agent_ids"):
            rc, err, rec = self._cli({field: self.SECOND,
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            self.assertNotIn(self.SECOND, ri.stand_discord_ids(rec), field)
            self.assertIsNone(rec["human_discord_id"], field)

    def test_an_explicit_sibling_provider_decides_both_ways(self):
        # Reviewer, 2026-08-30: a non-Discord sibling was ignored, so the bare
        # `id` fell through to the legacy rule and was published anyway.
        for provider, expect in (("discord", HUMAN), ("matrix", None),
                                 ("teams", None)):
            rc, err, rec = self._cli({"human": {"provider": provider,
                                                "id": HUMAN},
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            self.assertEqual(rec["human_discord_id"], expect, provider)

    def test_discord_must_be_a_word_not_a_substring(self):
        # The module's own rule ("words, not substrings") applied to the
        # provider too: `discordant` names no account.
        for field in ("discordant_human_id", "nondiscord_human_id"):
            rc, err, rec = self._cli({field: HUMAN,
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            self.assertIsNone(rec["human_discord_id"], field)

    def test_a_non_id_leaf_under_a_referent_is_not_mined(self):
        # Reviewer, 2026-08-30: the provider rule guarded `*_id` leaves only,
        # so `schema.md`'s documented non-evidence field was still mined.
        rc, err, rec = self._cli({"human": {"display_name": HUMAN},
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])

    def test_an_ancestor_provider_reaches_a_nested_leaf(self):
        # Only the leaf's immediate siblings were consulted, so a provider one
        # level up was invisible to a nested bare `id`.
        for provider, expect in (("matrix", None), ("discord", HUMAN)):
            rc, err, rec = self._cli({
                "human": {"provider": provider, "account": {"id": HUMAN}},
                "stand_discord_id": self.STAND})
            self.assertEqual(rc, 0, err)
            self.assertEqual(rec["human_discord_id"], expect, provider)

    def test_a_referent_naming_leaf_is_still_mined(self):
        # The control that keeps the rule from becoming "ids only":
        # `stand_status` names the referent in the LEAF and carries an id.
        rc, err, rec = self._cli({"stand_status": f"stand id {self.SECOND}"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(ri.stand_discord_id(rec), self.SECOND)

    ROOM = "1535008729106485288"

    def test_a_discord_provider_does_not_grant_every_sibling(self):
        # Reviewer, 2026-08-30: the provider names the NAMESPACE. Under it,
        # a room id was published as the human and could erase the real one.
        rc, err, rec = self._cli({"human": {"provider": "discord",
                                            "activity": {"rooms": [self.ROOM]}},
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])

    def test_a_room_beside_a_user_id_does_not_erase_the_human(self):
        # The damaging half: both were mined, the claims disagreed, and the
        # real human fell to unresolved.
        rc, err, rec = self._cli({
            "human": {"provider": "discord", "user_id": HUMAN,
                      "activity": {"rooms": [self.ROOM]}},
            "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["human_discord_id"], HUMAN)
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(rec)], [])

    def test_a_display_name_under_a_discord_provider_is_not_evidence(self):
        rc, err, rec = self._cli({"human": {"provider": "discord",
                                            "display_name": HUMAN},
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])

    def test_derived_basis_prose_is_never_fresh_evidence(self):
        # `id_basis` is this migration's OWN output. Re-reading it as input
        # lets a recorded reason re-cite the id it was written to explain.
        rc, err, rec = self._cli({
            ri.BASIS_FIELD: {"human_discord_id": [f"cited in `x` {HUMAN}"]},
            "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])

    def test_ordinary_metadata_is_not_a_shape_failure(self):
        # Reviewer, 2026-08-30: the STRING branch consulted the source rule and
        # the non-string branch did not, so a bool refused a valid roster.
        rc, err, _ = self._cli({"human": {"provider": "matrix", "active": True},
                                "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("SHAPE", err)

    def test_a_slot_value_the_collector_cannot_read_is_refused(self):
        # Validation called any JSON containing a snowflake readable, while the
        # collector mines strings — so explicit human evidence vanished at rc 0.
        for value in (1025828152183885925, {"value": HUMAN}, {}):
            rc, err, rec = self._cli({"discord_human_id": value,
                                      "stand_discord_id": self.STAND})
            self.assertEqual(rc, 5, f"{value!r}: {err}")
            self.assertIn("SHAPE x", err)
            self.assertIsNone(rec["human_discord_id"])

    def test_a_readable_string_slot_is_still_accepted(self):
        # Positive control: refusing every non-string would pass the case above
        # and reject the canonical spelling too.
        rc, err, rec = self._cli({"discord_human_id": HUMAN,
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_validation_and_collection_agree_on_a_wrapped_id(self):
        # The reviewer asked for ONE decision. A one-element list IS mined at
        # this path, so refusing it would accept and reject the same value.
        rc, err, rec = self._cli({"discord_human_id": [HUMAN],
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_a_compound_slot_refuses_the_members_the_collector_drops(self):
        # Reviewer, 2026-08-30: validation approximated the collector, so an
        # unreadable member rode along beside a readable one.
        for value in ([self.SECOND, 1529720369668292629],
                      [1529720369668292629], [{"value": self.SECOND}]):
            rc, err, _ = self._cli({"secondary_agent": {"ids": value},
                                    "stand_discord_id": self.STAND})
            self.assertEqual(rc, 5, f"{value!r}: {err}")
            self.assertIn("SHAPE x", err)

    def test_a_nested_member_is_validated_leaf_by_leaf(self):
        # Reviewer, 2026-08-30: only the OUTER list was flattened, so one
        # readable string made a whole nested member "readable".
        for entry in ({"secondary_agent": {"ids": [[self.SECOND,
                                                    1529720369668292629]]}},
                      {"discord_human_id": [[HUMAN, 1529720369668292629]]}):
            entry["stand_discord_id"] = self.STAND
            rc, err, _ = self._cli(entry)
            self.assertEqual(rc, 5, err)
            self.assertIn("SHAPE x", err)

    def test_a_nested_list_of_readable_ids_is_still_accepted(self):
        # Positive control: the collector descends lists without changing the
        # path, so rejecting nesting outright would refuse ids it does read.
        rc, err, rec = self._cli({"secondary_agent": {"ids": [[self.SECOND]]}})
        self.assertEqual(rc, 0, err)
        self.assertIn(self.SECOND, ri.stand_discord_ids(rec))

    def test_two_ids_in_a_singular_slot_resolve_to_neither(self):
        # Reviewer, 2026-08-30: both reached `stands` and the primary was the
        # first traversal result, so reversing the input changed the answer.
        first, second = [], []
        for order, sink in (([self.SECOND, BOT], first), ([BOT, self.SECOND], second)):
            rc, err, rec = self._cli({ri.STAND_FIELD: order})
            self.assertEqual(rc, 5, err)
            self.assertIsNone(ri.stand_discord_id(rec))
            sink.extend(u["id"] for u in ri.unresolved_discord_ids(rec))
        self.assertEqual(first, second, "input order reached the output")
        self.assertEqual(sorted(first), sorted([BOT, self.SECOND]))

    def test_an_over_full_slot_keeps_the_referent_it_stated(self):
        # Reviewer, 2026-08-30: I recorded the arbitrated ids with states=None,
        # so a contradicting source read as agreement and published a human.
        cfg = Path(self.tmp) / "tri.json"
        cfg.write_text(json.dumps({"people": {"x": {"discord": HUMAN,
                                                    "bots": []}}}))
        for order in ([HUMAN, BOT], [BOT, HUMAN]):
            src = Path(self.tmp) / "roster.json"
            src.write_text(json.dumps({"x": {ri.STAND_FIELD: order}}))
            out = Path(self.tmp) / "v2.json"
            sys.argv = ["m", "--roster", str(src), "--triage-config", str(cfg),
                        "--out", str(out)]
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                mig.main()
            rec = json.loads(out.read_text())["x"]
            self.assertIsNone(rec["human_discord_id"], order)
            self.assertIsNone(rec[ri.STAND_FIELD], order)
            self.assertEqual(sorted(u["id"] for u in
                                    ri.unresolved_discord_ids(rec)),
                             sorted([HUMAN, BOT]))

    def test_cardinality_counts_collected_ids_not_raw_json(self):
        # A snowflake in a record's metadata is not a second slot id; scanning
        # the rendering made the NOTE's content decide the outcome.
        for note in (f"room {self.ROOM}", "plain"):
            rc, err, rec = self._cli({ri.STAND_FIELD: {"id": self.SECOND,
                                                       "note": note}})
            self.assertEqual(rc, 0, f"{note!r}: {err}")
            self.assertEqual(ri.stand_discord_id(rec), self.SECOND, note)

    def test_an_ancestor_slot_keeps_its_referent_too(self):
        # Reviewer, 2026-08-30: arbitration read `path[-1]`, so a bare `id`
        # under `human` yielded states=None — later treated as agreement.
        cfg = Path(self.tmp) / "t.json"
        cfg.write_text(json.dumps({"people": {"x": {"bots": [BOT]}}}))
        for entry in ({"human_discord_id": [HUMAN, BOT]},
                      {"human": {"id": [HUMAN, BOT]}}):
            src = Path(self.tmp) / "r.json"
            src.write_text(json.dumps({"x": entry}))
            out = Path(self.tmp) / "v2.json"
            sys.argv = ["m", "--roster", str(src), "--triage-config", str(cfg),
                        "--out", str(out)]
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                mig.main()
            rec = json.loads(out.read_text())["x"]
            self.assertIsNone(rec["human_discord_id"], entry)
            self.assertIsNone(rec[ri.STAND_FIELD], entry)
            self.assertEqual(sorted(u["id"] for u in
                                    ri.unresolved_discord_ids(rec)),
                             sorted([HUMAN, BOT]), entry)

    def _cli_tri(self, entry, tri):
        src = Path(self.tmp) / "r.json"; src.write_text(json.dumps({"x": entry}))
        cfg = Path(self.tmp) / "t.json"; cfg.write_text(json.dumps({"people": {"x": tri}}))
        out = Path(self.tmp) / "v2.json"
        sys.argv = ["m", "--roster", str(src), "--triage-config", str(cfg),
                    "--out", str(out)]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = mig.main()
        return rc, err.getvalue(), json.loads(out.read_text())["x"]

    def test_two_stated_referents_cannot_agree_with_either(self):
        # Reviewer, 2026-08-30: a both-referent path collapsed to None, which
        # the agreement rule accepts. Earlier tests had no independent source.
        rc, err, rec = self._cli_tri(
            {"human_profile": {"secondary_agent": {"id": [HUMAN, BOT]}}},
            {"discord": HUMAN, "bots": []})
        self.assertEqual(rc, 5, err)
        self.assertIsNone(rec["human_discord_id"])
        self.assertIsNone(rec[ri.STAND_FIELD])
        self.assertEqual(sorted(u["id"] for u in ri.unresolved_discord_ids(rec)),
                         sorted([HUMAN, BOT]))

    def test_one_stated_referent_still_agrees_with_a_matching_slot(self):
        # Positive control: making every arbitrated id un-agreeable would pass
        # the case above and stop a single stated referent ever resolving.
        rc, _err, rec = self._cli_tri({"human": {"id": [HUMAN, BOT]}},
                                      {"discord": HUMAN, "bots": [BOT]})
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_member_order_cannot_decide_a_referent(self):
        # Reviewer, 2026-08-30: `arb_states` was a dict comprehension, so when
        # one id appears in TWO failures the later record overwrote the earlier.
        outs = []
        for entry in ({ri.HUMAN_FIELD: [HUMAN, BOT], ri.STAND_FIELD: [HUMAN, self.SECOND]},
                      {ri.STAND_FIELD: [HUMAN, self.SECOND], ri.HUMAN_FIELD: [HUMAN, BOT]}):
            rc, err, rec = self._cli_tri(entry, {"discord": HUMAN, "bots": []})
            self.assertEqual(rc, 5, err)
            outs.append((rec["human_discord_id"], rec[ri.STAND_FIELD],
                         sorted(u["id"] for u in ri.unresolved_discord_ids(rec))))
        self.assertEqual(outs[0], outs[1], "member order reached the output")
        self.assertIsNone(outs[0][0], "an id stated as BOTH was published")

    def test_a_repair_clears_the_carried_finding(self):
        # It latched: correcting the source still refused, because nothing ever
        # dropped a carried record.
        rc, err, rec = self._cli_tri({"secondary_agent": {"id": [HUMAN, BOT]}},
                                     {"discord": HUMAN, "bots": []})
        self.assertEqual(rc, 5, err)
        rec["secondary_agent"] = {"id": BOT}
        rc2, err2, fixed = self._cli_tri(rec, {"discord": HUMAN, "bots": []})
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(fixed[ri.STAND_FIELD], BOT)
        self.assertNotIn(ri.SHAPE_FIELD, fixed)

    def test_a_malformed_carried_value_never_synthesises_ids(self):
        # `arbitrated_ids` as a bare string was iterated per character and wrote
        # one fake unresolved id per digit.
        rc, err, rec = self._cli({"human": {"id": BOT},
                                  ri.SHAPE_FIELD: [{"path": "p", "kind": "str",
                                                    "reason": "r",
                                                    "arbitrated_ids": HUMAN}]})
        self.assertEqual([u["id"] for u in ri.unresolved_discord_ids(rec)], [], err)

    def test_a_non_dict_carried_record_does_not_crash_the_run(self):
        # Corrupt carried state is not evidence that no refusal existed, so
        # rc 0 is wrong here however the malformed container degrades.
        rc, err, rec = self._cli({"human": {"id": BOT},
                                  ri.SHAPE_FIELD: ["not-a-dict", 42]})
        self.assertEqual(rc, 5, err)
        self.assertTrue(rec.get(ri.SHAPE_FIELD), "corrupt state was erased")

    def test_an_older_scalar_record_canonicalises_to_one(self):
        # A pre-canonical writer's scalar and this one's list are the same
        # finding; uncanonicalised they persist as two forever.
        entry = {"human": {"id": BOT}, ri.SHAPE_FIELD: [
            {"path": "p", "kind": "k", "reason": "r", "arbitrated_states": "stand"},
            {"path": "p", "kind": "k", "reason": "r", "arbitrated_states": ["stand"]}]}
        for _ in range(3):
            rc, err, entry = self._cli(entry)
            self.assertEqual(len(entry.get(ri.SHAPE_FIELD) or []), 1, err)

    def test_the_writers_own_rewrite_is_not_a_repair(self):
        # Pass 1 rewrites the malformed slot; pass 2 must not read that
        # value as a user correction and clear the refusal.
        rc1, err1, rec1 = self._cli_tri({ri.STAND_FIELD: {"value": BOT}},
                                        {"bots": [BOT]})
        self.assertEqual(rc1, 5, err1)
        rc2, err2, _ = self._cli(rec1)
        self.assertEqual(rc2, 5, "the writer cleared its own refusal")

    def test_a_present_but_unusable_container_is_a_refusal_not_an_absence(self):
        # Coercing a non-list value to [] made corruption indistinguishable from
        # a field that was never there, and the refusal was silently dropped.
        for bad in ("corrupted", {"a": 1}, 7):
            rc1, err1, r1 = self._cli_tri({"human": {"id": HUMAN},
                                           ri.SHAPE_FIELD: bad},
                                          {"discord": HUMAN})
            self.assertEqual(rc1, 5, f"{bad!r} read as absent: {err1}")
            self.assertTrue(r1.get(ri.SHAPE_FIELD),
                            f"{bad!r} erased the refusal state")
            rc2, err2, r2 = self._cli(r1)
            self.assertEqual(rc2, 5, f"{bad!r} cleared on pass 2: {err2}")

    def test_the_control_an_absent_container_is_not_a_refusal(self):
        # Without this, treating absence as corruption would refuse every
        # roster that merely has no carried state.
        rc, err, rec = self._cli_tri({"human": {"id": HUMAN}}, {"discord": HUMAN})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec.get(ri.SHAPE_FIELD))

    def test_a_referent_without_an_id_decides_nothing_and_stays_bounded(self):
        # `arbitrated_states` with no ids names no id, so the classifier never
        # reads it; exempting it from the bound made the field unbounded.
        recs = [{"path": "p%03d" % i, "kind": "k", "reason": "r",
                 "arbitrated_states": ["stand"]} for i in range(80)]
        rc, err, rec = self._cli_tri({"human": {"id": HUMAN},
                                      ri.SHAPE_FIELD: recs}, {"discord": HUMAN})
        got = rec.get(ri.SHAPE_FIELD) or []
        self.assertLessEqual(len(got), ri.SHAPE_MAX, err)
        # Bounded is not discriminating: treating these as identity also stays
        # bounded, by COLLAPSING them. Diagnostics truncate, never aggregate.
        self.assertFalse(any(r.get("kind") == ri.OVERFLOW_KIND for r in got),
                         f"a referent with no id was treated as identity: {got}")
        self.assertEqual(len(got), ri.SHAPE_MAX,
                         f"diagnostics should fill the bound, got {len(got)}")

    def test_overflowing_arbitration_aggregates_and_stays_refused(self):
        # Identity facts may not be dropped, but they may not grow without
        # bound either: they collapse into one aggregate that keeps every id.
        recs = [{"path": "p%03d" % i, "kind": "k", "reason": "r",
                 "arbitrated_ids": [BOT], "arbitrated_states": ["stand"]}
                for i in range(80)]
        rc, err, rec = self._cli_tri({"human": {"id": HUMAN},
                                      ri.SHAPE_FIELD: recs}, {"discord": HUMAN})
        got = rec.get(ri.SHAPE_FIELD) or []
        self.assertLessEqual(len(got), ri.SHAPE_MAX, err)
        self.assertTrue(any(r.get("kind") == ri.OVERFLOW_KIND for r in got),
                        f"no overflow marker: {got}")
        self.assertTrue(any(BOT in (r.get("arbitrated_ids") or []) for r in got),
                        "the aggregate lost the contested id")

    def test_the_bound_cannot_decide_an_identity_ACROSS_PASSES(self):
        # The cap is diagnostic history; an identity-bearing record is the only
        # surviving evidence of a contested id once the source is overwritten.
        import collections
        for slot_last in (True, False):
            e = collections.OrderedDict()
            e["human"] = {"id": HUMAN}
            if not slot_last:
                e[ri.STAND_FIELD] = [HUMAN, BOT]
            for i in range(ri.SHAPE_MAX):
                e["bad%02d_discord_id" % i] = 12
            if slot_last:
                e[ri.STAND_FIELD] = [HUMAN, BOT]
            rc1, err1, r1 = self._cli_tri(e, {"discord": HUMAN})
            self.assertEqual(rc1, 5, err1)
            self.assertTrue(any(ri.bears_identity(x)
                                for x in (r1.get(ri.SHAPE_FIELD) or [])),
                            "the bound cut the arbitration fact (slot_last=%s)"
                            % slot_last)
            rc2, err2, r2 = self._cli(r1)
            self.assertEqual(rc2, 5, err2)
            self.assertIsNone(r2.get(ri.HUMAN_FIELD),
                              "pass 2 published a contested id (slot_last=%s)"
                              % slot_last)

    def test_the_bound_cannot_decide_an_identity(self):
        # The cap truncated LIVE findings before arbitration, so whether the
        # over-full slot survived depended on member order.
        noise = {f"bad{i}_discord_id": 12 for i in range(ri.SHAPE_MAX)}
        outs = []
        for entry in ({ri.STAND_FIELD: [HUMAN, BOT], **noise},
                      {**noise, ri.STAND_FIELD: [HUMAN, BOT]}):
            rc, err, rec = self._cli_tri(entry, {"discord": HUMAN, "bots": []})
            self.assertEqual(rc, 5, err)
            outs.append((rec[ri.STAND_FIELD],
                         sorted(u["id"] for u in ri.unresolved_discord_ids(rec))))
        self.assertEqual(outs[0], outs[1], "the bound let member order decide")
        self.assertIsNone(outs[0][0], "an over-full slot published its id")

    def test_cross_record_union_covers_the_stand_side_too(self):
        # Reviewer: a STAND-precedence overwrite mutant passed every test,
        # because the member-order case supplied only HUMAN evidence.
        for tri in ({"discord": HUMAN, "bots": []}, {"bots": [HUMAN]}):
            outs = []
            for entry in ({ri.HUMAN_FIELD: [HUMAN, BOT], ri.STAND_FIELD: [HUMAN, self.SECOND]},
                          {ri.STAND_FIELD: [HUMAN, self.SECOND], ri.HUMAN_FIELD: [HUMAN, BOT]}):
                rc, err, rec = self._cli_tri(entry, tri)
                outs.append((rec["human_discord_id"], rec[ri.STAND_FIELD]))
            self.assertEqual(outs[0], outs[1], tri)
            self.assertEqual(outs[0], (None, None), tri)

    def test_the_carried_list_is_bounded(self):
        # Unbounded carried state can grow a roster indefinitely.
        big = [{"path": f"p{i}", "kind": "k", "reason": f"r{i}"} for i in range(80)]
        rc, err, rec = self._cli({"human": {"id": BOT}, ri.SHAPE_FIELD: big})
        self.assertLessEqual(len(rec.get(ri.SHAPE_FIELD) or []), ri.SHAPE_MAX, err)

    def test_the_carried_refusal_converges_across_passes(self):
        # `id_shape_failures` grew 1, 2, 3: the source field survives, so each
        # pass re-detected the finding and appended it beside the carried copy.
        entry = {"secondary_agent": {"id": [HUMAN, BOT]}}
        counts = []
        for _ in range(3):
            rc, err, entry = self._cli_tri(entry, {"discord": HUMAN, "bots": []})
            self.assertEqual(rc, 5, err)
            counts.append(len(entry.get(mig.SHAPE_FIELD) or []))
        self.assertEqual(counts, [1, 1, 1], counts)

    def test_a_refusal_survives_re_migration(self):
        # The writer overwrites the malformed slot, so pass 2 saw a clean doc
        # and returned 0 — a refusal quietly becoming a success.
        rc1, err1, rec1 = self._cli({ri.STAND_FIELD: {"value": HUMAN}})
        self.assertEqual(rc1, 5, err1)
        self.assertIn("SHAPE x", err1)
        src = Path(self.tmp) / "again.json"
        src.write_text(json.dumps({"x": rec1}))
        out = Path(self.tmp) / "again.v2.json"
        sys.argv = ["m", "--roster", str(src), "--out", str(out)]
        err2 = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err2):
            rc2 = mig.main()
        self.assertEqual(rc2, 5, "a second migration reported success")
        self.assertIn("SHAPE", err2.getvalue())

    def test_a_clean_roster_gains_no_refusal_field(self):
        # Negative control: carrying the field unconditionally would make every
        # migrated doc look refused forever.
        rc, err, rec = self._cli({ri.STAND_FIELD: self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertNotIn(mig.SHAPE_FIELD, rec)

    def test_a_foreign_ancestor_cannot_launder_a_bare_id(self):
        # Refused as a leaf, then accepted by moving `id` one level down.
        # Assert the OUTPUT: with a valid Stand there is no gap, so rc is 0.
        rc, err, rec = self._cli({"telegram_human": {"id": HUMAN},
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])
        self.assertNotIn(HUMAN, [u["id"] for u in
                                 ri.unresolved_discord_ids(rec)])

    def test_a_room_id_under_a_discord_provider_is_not_an_identity(self):
        # `schema.md` defines room_id as a ROOM. A provider names the
        # namespace, not that every id under it identifies a person.
        rc, err, rec = self._cli({
            "human": {"provider": "discord", "activity": {"room_id": self.ROOM}},
            "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(rec["human_discord_id"])
        self.assertNotIn(self.ROOM, ri.stand_discord_ids(rec))

    def test_a_schema_record_member_is_still_read(self):
        # Positive control: `{"id": ...}` IS mined, and the v2 writer emits
        # exactly this shape — refusing it would break re-migration.
        rc, err, rec = self._cli({"secondary_agent": {"ids": [{"id": self.SECOND}]},
                                  "stand_discord_id": self.STAND})
        self.assertEqual(rc, 0, err)
        self.assertIn(self.SECOND, ri.stand_discord_ids(rec))

    def test_the_one_measured_legacy_spelling_still_resolves(self):
        # The allowlist, and the reason it is not empty: a bare `id` under a
        # referent ancestor states no provider and is documented in-tree.
        rc, err, rec = self._cli({"human": {"id": HUMAN}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec["human_discord_id"], HUMAN)

    def test_a_plural_slot_refuses_a_member_that_holds_no_id(self):
        # Empty is the slot being empty; junk INSIDE it is the same defect one
        # axis over, and the readable sibling must still be collected.
        rc, err, rec = self._cli({ri.STAND_FIELD: self.STAND,
                                  "secondary_agent": {"ids": [self.STAND,
                                                              "junk"]}})
        self.assertEqual(rc, 5)
        self.assertIn("SHAPE x", err)
        self.assertEqual(rec[ri.STAND_FIELD], self.STAND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
