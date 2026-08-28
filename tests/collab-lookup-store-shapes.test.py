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

    def test_no_match_returns_empty_not_invented(self):
        with tempfile.TemporaryDirectory() as t:
            d = store(t, roster=ROSTER)
            self.assertEqual(lk.match(lk.load_roster(d), "no-such-login"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
