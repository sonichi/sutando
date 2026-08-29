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


if __name__ == "__main__":
    unittest.main(verbosity=2)
