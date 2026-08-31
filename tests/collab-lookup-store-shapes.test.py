#!/usr/bin/env python3
"""lookup.py must resolve against every live store shape (#3517 review).

Every case here is a store that existed on a real host on 2026-08-28 and
broke a prior revision: wrapped {quick_lookup:}, flat {people:}, MALFORMED
yaml, roster-only, and a multi-host merge leaving one person under two keys.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "lookup", Path(__file__).resolve().parents[1]
    / "skills" / "collaboration-intelligence" / "scripts" / "lookup.py")
lk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lk)


def store(tmp, yaml_text=None, roster=None):
    d = Path(tmp) / "data" / "collaboration-intelligence"
    d.mkdir(parents=True)
    if yaml_text is not None:
        (d / "quick-lookup.yaml").write_text(yaml_text)
    if roster is not None:
        (d / "reviewer-stands.json").write_text(json.dumps(roster))
    return d


ROSTER = {"rui": {"github": "john-the-dev", "stand": "@sutando-rui:ag2.space",
                  "human": "@ruiwangwarm:ag2.space", "allowlisted": True}}


class StoreShapes(unittest.TestCase):
    def test_wrapped_schema_resolves_recent_entities(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "quick_lookup:\n  recent_entities:\n"
                         "    - entity_id: wrapped-test\n"
                         "      agent_mxid: '@wrapped:ag2.space'\n")
            q, _ = lk.load(d)
            hits = lk.match(q.get("recent_entities") or q.get("people") or [], "wrapped-test")
            self.assertEqual([h["entity_id"] for h in hits], ["wrapped-test"])

    def test_flat_people_schema_resolves(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people:\n  - entity_id: keweichen\n"
                         "    agent_mxid: '@kchen6red-m1max.agent:ag2.space'\n"
                         "rooms: []\n")
            q, _ = lk.load(d)
            hits = lk.match(q.get("recent_entities") or q.get("people") or [], "keweichen")
            self.assertEqual(len(hits), 1)

    def test_flat_id_who_fields_match(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people:\n  - id: 'gh:qingyun-wu'\n"
                         "    who: 'reviewer on the server-PR stack'\n")
            q, _ = lk.load(d)
            rows = q.get("recent_entities") or q.get("people") or []
            self.assertEqual(len(lk.match(rows, "qingyun-wu")), 1)
            self.assertEqual(len(lk.match(rows, "server-PR")), 1)

    def test_malformed_yaml_degrades_to_roster(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people:\n    broken no colon here\n    another: [unclosed\n",
                      roster=ROSTER)
            q, _ = lk.load(d)          # must not raise
            self.assertEqual(q, {})
            rows = lk.load_roster(d)
            hits = lk.match(rows, "john-the-dev")
            self.assertEqual(hits[0]["agent_mxid"], "@sutando-rui:ag2.space")

    def test_roster_github_field_not_key(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=ROSTER)
            hits = lk.match(lk.load_roster(d), "john-the-dev")
            self.assertEqual(hits[0]["entity_id"], "rui")

    def test_stand_bearing_row_orders_first(self):
        with tempfile.TemporaryDirectory() as t:
            r = dict(ROSTER)
            r["john-the-dev"] = {"human_name": "Rui"}   # field-poor merge duplicate
            d = store(t, roster=r)
            hits = lk.match(lk.load_roster(d), "john-the-dev")
            self.assertEqual(len(hits), 2)
            self.assertEqual(hits[0]["agent_mxid"], "@sutando-rui:ag2.space")

    def test_entities_yaml_alone_loads_without_quick_lookup(self):
        # core-3's control: entities.yaml with NO quick-lookup.yaml must still
        # load — the read must not nest inside the other store's existence check.
        with tempfile.TemporaryDirectory() as t:
            d = store(t)                             # writes NEITHER yaml
            (d / "entities.yaml").write_text(
                "entities:\n  - entity_id: solo\n    identities:\n"
                "      - provider: github\n        provider_id: solo-login\n")
            q, ents = lk.load(d)
            self.assertEqual(len(ents), 1)

    def test_malformed_entities_yaml_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people: []\n")
            (d / "entities.yaml").write_text("entities:\n    broken [unclosed\n    no: colon here\n")
            q, ents = lk.load(d)
            self.assertEqual(ents, [])

    def test_roster_absent_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people: []\n")
            self.assertEqual(lk.load_roster(d), [])

    def test_roster_non_dict_values_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            r = dict(ROSTER)
            r["_merge_log"] = ["a merge event"]     # real shape: sync writes a list here
            d = store(t, roster=r)
            rows = lk.load_roster(d)
            self.assertEqual([x["entity_id"] for x in rows], ["rui"])

    def test_main_empty_store_prints_map_empty(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data" / "collaboration-intelligence"
            d.mkdir(parents=True)                    # store dir exists, holds nothing
            orig_store, orig_argv = lk.store, sys.argv
            out = io.StringIO()
            try:
                lk.store = lambda: d
                sys.argv = ["lookup.py", "anyone"]
                with contextlib.redirect_stdout(out):
                    rc = lk.main()
            finally:
                lk.store, sys.argv = orig_store, orig_argv
            self.assertEqual(rc, 0)
            self.assertIn("MAP EMPTY", out.getvalue())

    def test_main_resolves_through_full_path(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=ROSTER)
            orig_store, orig_argv = lk.store, sys.argv
            out = io.StringIO()
            try:
                lk.store = lambda: d
                sys.argv = ["lookup.py", "john-the-dev"]
                with contextlib.redirect_stdout(out):
                    rc = lk.main()
            finally:
                lk.store, sys.argv = orig_store, orig_argv
            self.assertEqual(rc, 0)
            self.assertIn("@sutando-rui:ag2.space", out.getvalue())

    def test_identity_matches_on_the_schema_documented_user_id(self):
        # schema.md documents `user_id`; the reader only read `provider_id`, so a
        # store written to the schema returned NO MATCH for every identifier.
        ents = [{"entity_id": "person-x",
                 "identities": [{"provider": "github", "user_id": "octo-dev"}]}]
        rows = [{"entity_id": "person-x", "id": "person-x", "one_line": "", "agent_mxid": ""}]
        self.assertEqual([r["entity_id"] for r in lk.match(rows, "octo-dev", ents)],
                         ["person-x"])

    def test_main_renders_a_schema_user_id_identity(self):
        # Covers the RENDER site: matching alone never executes the id line.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as t:
            d = store(t, yaml_text=(
                "quick_lookup:\n"
                "  updated_at: 2026-08-29T00:00:00Z\n"
                "  recent_entities:\n"
                "    - entity_id: person-x\n"
                "      kind: human\n"
                "      one_line: schema-faithful store\n"))
            (d / "entities.yaml").write_text(
                "entities:\n"
                "  - entity_id: person-x\n"
                "    identities:\n"
                "      - {provider: github, user_id: octo-dev}\n")
            orig_store, orig_argv = lk.store, sys.argv
            out = io.StringIO()
            try:
                lk.store = lambda: d
                sys.argv = ["lookup.py", "octo-dev"]
                with contextlib.redirect_stdout(out):
                    rc = lk.main()
            finally:
                lk.store, sys.argv = orig_store, orig_argv
            self.assertEqual(rc, 0)
            # Resolved by the documented key, and the id is actually printed.
            self.assertIn("github=octo-dev", out.getvalue())

    def test_identity_still_matches_on_provider_id(self):
        # Existing stores use provider_id; honouring user_id must not drop them.
        ents = [{"entity_id": "person-y",
                 "identities": [{"provider": "github", "provider_id": "hubot"}]}]
        rows = [{"entity_id": "person-y", "id": "person-y", "one_line": "", "agent_mxid": ""}]
        self.assertEqual([r["entity_id"] for r in lk.match(rows, "hubot", ents)],
                         ["person-y"])

    def test_no_match_returns_empty_not_invented(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=ROSTER)
            self.assertEqual(lk.match(lk.load_roster(d), "no-such-login"), [])


class AMigratedDocumentExposesNoMetadataAsAPerson(unittest.TestCase):
    """The reader is activated on the v2 sidecar, so its reserved block matters."""

    V2 = {"_schema": {"version": 2, "contract": "reserved"},
          "x": {"github": "gh-x", "human": "X", "stand": "@sutando-x:ag2.space"}}

    def test_the_reserved_schema_block_is_not_a_reviewer(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=self.V2)
            ids = [r["entity_id"] for r in lk.load_roster(d)]
            self.assertNotIn("_schema", ids, "metadata rendered as an addressable person")
            self.assertEqual(ids, ["x"])

    def test_the_metadata_rule_has_ONE_owner(self):
        # roster_identity.is_person_key is the owner; a byte-equivalent private
        # copy passes every behaviour test and drifts the moment the rule moves.
        import pathlib as _p
        # EVERY reader of the promoted roster, not just the one that prompted
        # this test: a scan naming one file lets the next copy in unseen.
        scripts = _p.Path(lk.__file__).parent
        for name in ("lookup.py", "notify_reviewers.py"):
            src = (scripts / name).read_text()
            self.assertNotIn('startswith("_")', src,
                             f"{name} carries its own copy of the metadata rule")
            self.assertIn("is_person_key", src, f"{name} must call the owner")

    def test_the_owner_itself_is_the_only_definition(self):
        # The negative control: the scan above must not be satisfiable by
        # deleting the rule everywhere, so pin that the owner still defines it.
        import pathlib as _p
        owner = (_p.Path(lk.__file__).parent / "roster_identity.py").read_text()
        self.assertIn("def is_person_key", owner)
        self.assertIn('startswith("_")', owner,
                      "the owner is where the rule is allowed to live")

    def test_a_real_person_in_the_same_document_still_loads(self):
        # The negative control: filtering must not empty the roster.
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=self.V2)
            self.assertEqual(len(lk.match(lk.load_roster(d), "gh-x")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
