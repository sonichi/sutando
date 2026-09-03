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

    def test_listing_names_an_id_who_row_instead_of_a_blank_column(self):
        """--all must not render a row that match() can find as nameless.

        The renderer read only `entity_id`; a real host's quick-lookup.yaml
        keys people as `id`, so four rows listed as blank names against a
        populated Stand column.
        """
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people:\n  - id: 'gh:qingyun-wu'\n"
                         "    who: 'reviewer on the server-PR stack'\n")
            orig_store, orig_argv = lk.store, sys.argv
            lk.store, sys.argv = (lambda: d), ["lookup.py", "--all"]
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    lk.main()
            finally:
                lk.store, sys.argv = orig_store, orig_argv
        body = [l for l in buf.getvalue().splitlines()
                if l.startswith("  ") and "updated_at" not in l]
        self.assertEqual(len(body), 1, buf.getvalue())
        self.assertIn("gh:qingyun-wu", body[0])

    def test_a_query_renders_the_name_and_role_of_an_id_who_row(self):
        """The per-query detail path had the same blind spot as --all: it read
        entity_id/one_line, so a matched quick-lookup row printed a blank name
        above a blank role — on the one surface used to decide who to address.
        """
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as t:
            d = store(t, "people:\n  - id: 'gh:qingyun-wu'\n"
                         "    who: 'reviewer on the server-PR stack'\n")
            orig_store, orig_argv = lk.store, sys.argv
            lk.store, sys.argv = (lambda: d), ["lookup.py", "gh:qingyun-wu"]
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    lk.main()
            finally:
                lk.store, sys.argv = orig_store, orig_argv
        out = buf.getvalue()
        self.assertIn("gh:qingyun-wu", out, out)
        self.assertIn("reviewer on the server-PR stack", out, out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
