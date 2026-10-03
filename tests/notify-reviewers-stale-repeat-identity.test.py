#!/usr/bin/env python3
"""Stale-repeat detection resolves a person the way routing and parking do.

`identity_components` makes two rows sharing a Discord id ONE person, and
`component_resolver` is what routing and park admission consult. `_stale_repeat_ask`
canonicalised through `_actor_map` instead, which links only rows that DECLARE
`same_actor_as`. So a person asked under their Matrix alias read as never-asked
under their Discord alias: re-pinged, and offered back as a widen target.

The distinct-person rows are the control -- an implementation that called every
target stale would pass the first two assertions and fail the third.

Run: python3 tests/notify-reviewers-stale-repeat-identity.test.py
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_LED = Path(tempfile.mkdtemp(prefix="nr-stale-identity-")) / "ledger.jsonl"
os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(_LED)
_spec = importlib.util.spec_from_file_location(
    "nr", ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

MSG = "please review https://github.com/o/r/pull/7"
# mx and dc are ONE human: same discord id, no same_actor_as between them.
ROSTER = {
    "mx": {"stand": "@mx:x", "room": "!r:x", "discord_id": "123",
           "home_channel": "c1", "allowlisted": True},
    "dc": {"discord_id": "123", "home_channel": "c1", "allowlisted": True},
    "other": {"stand": "@other:x", "room": "!r:x", "allowlisted": True},
}


def _aged(hours=3):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.isoformat().replace("+00:00", "Z")


def _seed_confirmed_ask_to(who):
    _LED.write_text(json.dumps({
        "repo": "o/r", "pr": "7", "reviewer": who, "actor": who,
        "outcome": "confirmed", "ts": _aged(),
    }) + "\n")


def _stale(selected):
    return nr._stale_repeat_ask(MSG, [{"name": selected}], ROSTER)[0]


class OneHumanIsStaleUnderEitherAlias(unittest.TestCase):
    def setUp(self):
        _seed_confirmed_ask_to("mx")

    def test_the_asked_alias_is_stale(self):
        self.assertIs(_stale("mx"), True, "the alias actually asked must refuse")

    def test_the_other_alias_of_the_same_person_is_also_stale(self):
        self.assertIs(
            _stale("dc"), True,
            "mx and dc share discord id 123, so one ask reached this human")

    def test_a_distinct_person_is_not_stale(self):
        # Control: an implementation that refused everything passes the two above.
        self.assertIs(_stale("other"), False,
                      "an unasked, unlinked person must remain askable")

    def test_the_linked_alias_is_not_offered_as_a_widen_target(self):
        _refuse, why = nr._stale_repeat_ask(MSG, [{"name": "mx"}], ROSTER)
        self.assertNotIn("dc", why,
                         "widening to the same human is not widening")


if __name__ == "__main__":
    unittest.main(verbosity=2)
