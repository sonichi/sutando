#!/usr/bin/env python3
"""A blank local field must not erase what a peer row STATED.

`roster_union._usable` and `notify_reviewers.stated_reason` both read a blank or
whitespace `refusal_basis`/`note` as ABSENT, and roster-union's identity reader
reads a blank `gh`/`github` the same way. The promotion overlay disagreed: it
copied every non-`None` local value onto the promoted peer row, so a local
placeholder carrying `refusal_basis: ""` blanked a peer's `"DO NOT ROUTE"` and
the reviewer was then routed.

Measured at three points, because "fixed" and "not a regression" are different
claims and only the first group is a regression:

  REGRESSED    the two refusal arms FAIL at the promoting head and PASS at
               origin/main, which withholds correctly. A blank erasing a stated
               refusal is behaviour that got WORSE, not merely unfinished.
  INCOMPLETE   the reason-reaches-the-operator and identity arms fail at BOTH,
               each for its own reason: origin/main never promotes, so the peer
               row it preserves is only reachable under a suffix.
  MUST-WORK    a real local value MUST still overlay and `False` is a value, so
               "never overlay" and "overlay only what is truthy" both fail here.
               These exercise promotion itself and so do not apply to
               origin/main, which has none.

Run: python3 tests/roster-union-blank-overlay.test.py   (stdlib only)
"""
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parents[1] / "skills"
           / "collaboration-intelligence" / "scripts")

PEER_REFUSAL = "DO NOT ROUTE"
PEER_ROUTE = {"stand": "@peer:x", "room": "!peer:x"}
BLANKS = ("", "   ", "\t\n")
# A text field states nothing when it holds one of these: `stated_reason` prints
# no reason for any of them, so neither may overlay a peer's stated refusal.
NON_STRINGS = (False, 0, ["x"], {"a": 1})
IDENTITY_FIELDS = ("gh", "github")
# Spelled independently of the source so the equality arms below can disagree.
REFUSAL_FIELDS = ("refusal_basis", "note")
# Text-typed, but metadata: a caveat prints, a login identifies. Neither refuses.
NON_REFUSAL_TEXT = ("authority_caveat", "same_actor_as") + IDENTITY_FIELDS
TEXT_FIELDS = REFUSAL_FIELDS + NON_REFUSAL_TEXT


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: notify_reviewers imports roster_union by name, and
    # an unregistered module would be re-executed as a second, unpatched object.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BlankOverlay(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        self.ru = _load("roster_union", "roster_union.py")
        self.nr = _load("notify_reviewers", "notify_reviewers.py")
        self.dir = Path(tempfile.mkdtemp())

    def union(self, *rosters):
        """(host, {key: row}) pairs, nearest first -> the merged roster."""
        paths = []
        for host, data in rosters:
            p = self.dir / (host + ".json")
            p.write_text(json.dumps(data))
            paths.append((host, p))
        return self.ru.roster_union(paths)

    def resolve(self, merged, name="reviewer"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            targets, rc = self.nr.resolve([name], merged)
        return targets, rc, err.getvalue()

    def three_rosters(self, local_basis, field="refusal_basis"):
        """The reviewer's measured case: blank local, peer refusal, routable legacy."""
        return self.union(
            ("local", {"reviewer": {"stand": "", "room": "",
                                    field: local_basis}}),
            ("PEER_REFUSAL", {"reviewer": {"stand": "", "room": "",
                                           field: PEER_REFUSAL}}),
            ("LEGACY", {"reviewer": dict(PEER_ROUTE)}),
        )

    # --- arms 1-3: the refusal must survive a blank local field ----------
    def test_a_blank_local_basis_does_not_route_a_refused_reviewer(self):
        for blank in BLANKS:
            with self.subTest(local_basis=blank):
                targets, rc, _ = self.resolve(self.three_rosters(blank))
                self.assertEqual(
                    (len(targets), rc), (0, 3),
                    "a peer stated DO NOT ROUTE and a blank local "
                    f"{blank!r} erased it — the reviewer gets routed")

    def test_the_stated_refusal_is_still_readable_in_the_union(self):
        for blank in BLANKS:
            with self.subTest(local_basis=blank):
                merged = self.three_rosters(blank)
                self.assertIn(PEER_REFUSAL, json.dumps(merged),
                              f"refusal lost entirely; keys: {sorted(merged)}")

    def test_the_refusal_reason_reaches_the_operator(self):
        """Withholding silently reads as a data gap, and the obvious repair is to
        fill the fields in — which converts the refusal into a route (#3468)."""
        _, _, err = self.resolve(self.three_rosters(""))
        self.assertIn(PEER_REFUSAL, err)

    # --- the same three rosters, non-string local text fields ------------
    def test_a_NON_STRING_local_text_field_does_not_route_a_refused_reviewer(self):
        """`is_declared` calls every non-`None` present, which is right for
        `allowlisted` and wrong here: a list states no reason a reader can read,
        so overlaying it leaves a refusal that prints nothing and then routes."""
        for field in REFUSAL_FIELDS:
            for value in NON_STRINGS:
                with self.subTest(field=field, local=value):
                    merged = self.three_rosters(value, field)
                    targets, rc, _ = self.resolve(merged)
                    self.assertEqual(
                        (len(targets), rc), (0, 3),
                        f"a peer stated {PEER_REFUSAL} and a local {value!r} "
                        f"in {field} erased it — {targets} got routed")

    def test_a_non_string_local_text_field_leaves_the_reason_readable(self):
        for field in REFUSAL_FIELDS:
            for value in NON_STRINGS:
                with self.subTest(field=field, local=value):
                    merged = self.three_rosters(value, field)
                    self.assertEqual(self.nr.stated_reason(merged["reviewer"]),
                                     PEER_REFUSAL)
                    _, _, err = self.resolve(merged)
                    self.assertIn(PEER_REFUSAL, err,
                                  "the refusal withholds but states no reason, "
                                  "so filling the fields in overrides it")

    def test_the_routable_row_loses_to_the_refusal_but_is_still_kept(self):
        """The erased refusal let a THIRD roster promote over the same key, so
        the routable row took the bare key instead of a suffix — and the second
        promotion overwrote the only `@local` copy of the row this host wrote."""
        merged = self.three_rosters(False)
        self.assertIn("reviewer@LEGACY", merged,
                      f"the routable row won the bare key: {sorted(merged)}")
        self.assertIs(merged["reviewer@local"].get("refusal_basis"), False,
                      "the local row was replaced by a promoted copy")

    # --- arms 4-6: a real value MUST still overlay -----------------------
    def test_a_real_local_value_still_overlays_onto_the_promoted_row(self):
        """Blanket "never overlay" would pass arms 1-3 and fail here.

        A caveat, not a note: `note` is itself a refusal, so a row carrying one
        is never promoted over and would measure the wrong thing.
        """
        merged = self.union(
            ("local", {"reviewer": {"stand": "", "room": "",
                                    "room_caveat": "local knows this"}}),
            ("peer", {"reviewer": dict(PEER_ROUTE)}),
        )
        row = merged["reviewer"]
        self.assertEqual(row.get("room_caveat"), "local knows this")
        self.assertEqual(row.get("stand"), PEER_ROUTE["stand"],
                         "peer routing must still win the promotion")
        _, _, err = self.resolve(merged)
        self.assertIn("local knows this", err, "the overlaid caveat is not printed")

    def test_allowlisted_false_still_overlays(self):
        """`False` is a stated refusal, not an absence: a truthiness test loses it."""
        merged = self.union(
            ("local", {"reviewer": {"stand": "", "room": "", "allowlisted": False}}),
            ("peer", {"reviewer": dict(PEER_ROUTE)}),
        )
        self.assertIs(merged["reviewer"].get("allowlisted"), False)
        _, rc, _ = self.resolve(merged)
        self.assertEqual(rc, 4, "off-allowlist must still be refused")

    def test_CONTROL_a_non_string_still_overlays_a_field_that_is_not_text(self):
        """Making the predicate strict everywhere would pass every arm above and
        fail here — only a TEXT field asks for text."""
        for value in NON_STRINGS:
            with self.subTest(local=value):
                merged = self.union(
                    ("local", {"reviewer": {"stand": "", "room": "",
                                            "allowlisted": value}}),
                    ("peer", {"reviewer": dict(PEER_ROUTE, allowlisted=True)}),
                )
                self.assertEqual(merged["reviewer"].get("allowlisted"), value)

    # --- arms 7-9: the same overlay, on identity -------------------------
    def _resolved_login(self, merged, name="reviewer"):
        self.nr._is_github_user = lambda login: True
        return self.nr._github_login(name, merged)[0]

    def test_a_blank_local_alias_does_not_erase_a_peer_identity(self):
        for field in ("gh", "github"):
            for blank in BLANKS:
                with self.subTest(field=field, local=blank):
                    merged = self.union(
                        ("local", {"reviewer": {"stand": "", "room": "",
                                                field: blank}}),
                        ("peer", dict(reviewer=dict(PEER_ROUTE,
                                                    **{field: "peer-owner"}))),
                    )
                    self.assertEqual(self._resolved_login(merged), "peer-owner",
                                     "a blank alias resolved to the roster KEY; "
                                     "capability checks then run on a collision")

    def test_a_blank_local_same_actor_as_does_not_erase_the_peer_one(self):
        for blank in BLANKS:
            with self.subTest(local=blank):
                merged = self.union(
                    ("local", {"reviewer": {"stand": "", "room": "",
                                            "same_actor_as": blank}}),
                    ("peer", {"reviewer": dict(PEER_ROUTE,
                                               same_actor_as="peer-owner")}),
                )
                self.assertEqual(self._resolved_login(merged), "peer-owner")

    def test_CONTROL_a_real_local_alias_still_wins(self):
        """The local roster is still nearest: a stated local alias must overlay."""
        merged = self.union(
            ("local", {"reviewer": {"stand": "", "room": "", "gh": "local-owner"}}),
            ("peer", {"reviewer": dict(PEER_ROUTE, gh="peer-owner")}),
        )
        self.assertEqual(self._resolved_login(merged), "local-owner")
    def test_a_local_metadata_row_still_ROUTES_and_keeps_its_own_field(self):
        """The measured regression: a local row holding only metadata must not
        withhold the peer route. Asserts DELIVERY as well as the kept value —
        refusing the whole row preserves the value too, so the login alone
        cannot tell the two apart."""
        for field in NON_REFUSAL_TEXT:
            with self.subTest(field=field):
                merged = self.union(
                    ("local", {"reviewer": {"stand": None, "room": None,
                                            field: "local-owner"}}),
                    ("PEER", {"reviewer": dict(PEER_ROUTE)}),
                )
                targets, rc, _ = self.resolve(merged)
                self.assertEqual(
                    (len(targets), rc), (1, 0),
                    f"a local {field!r} withheld the peer route entirely")
                self.assertEqual(targets[0]["stand"], PEER_ROUTE["stand"])
                kept = [r for k, r in merged.items()
                        if k == "reviewer" or k.startswith("reviewer@")]
                self.assertTrue(
                    any(r.get(field) == "local-owner" for r in kept),
                    f"the local {field!r} was dropped: {kept}")

    def test_only_the_refusal_fields_can_withhold_a_route(self):
        """Type is not intent: widening the text list must not widen refusal."""
        self.assertEqual(self.ru.REFUSAL_FIELDS, REFUSAL_FIELDS)
        for f in NON_REFUSAL_TEXT:
            self.assertIn(f, self.ru.TEXT_FIELDS)
            self.assertNotIn(f, self.ru.REFUSAL_FIELDS)




class SharedPresencePredicate(unittest.TestCase):
    """The one predicate the three sites now share, pinned directly.

    Unit-level and therefore new-API-only: origin/main has no such helper, so
    only the behavioural class above is measurable against it.
    """

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        self.ru = _load("roster_union", "roster_union.py")

    def test_declared_reads_blank_whitespace_and_non_string_as_nothing(self):
        for value in ("", "   ", "\t\n", None, False, 0, ["x"], {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(self.ru.declared(value), "")

    def test_declared_strips_the_text_it_returns(self):
        self.assertEqual(self.ru.declared("  DO NOT ROUTE  "), "DO NOT ROUTE")

    def test_is_declared_keeps_non_strings_that_state_something(self):
        for value in (False, 0, ["x"], {"a": 1}, "x"):
            with self.subTest(value=value):
                self.assertTrue(self.ru.is_declared(value))

    def test_is_declared_rejects_none_and_blank_strings(self):
        for value in (None, "", "   ", "\t\n"):
            with self.subTest(value=value):
                self.assertFalse(self.ru.is_declared(value))

    def test_the_readers_answer_through_the_same_predicate(self):
        """Divergence here is the defect: one site's absence is another's data."""
        nr = _load("notify_reviewers", "notify_reviewers.py")
        # Non-strings included: a list-valued basis made the union call a row a
        # refusal while the notifier printed no reason for it.
        for value in BLANKS + (None, False, 0, ["x"], {"a": 1}):
            with self.subTest(value=value):
                row = {"stand": "", "room": "", "refusal_basis": value}
                self.assertEqual(nr.stated_reason(row), "")
                self.assertFalse(self.ru._usable(row),
                                 "the union calls this a refusal that never prints")
                self.assertEqual(self.ru.roster_login({"gh": value}), ("", ""))

    def test_the_rule_is_spelled_exactly_once(self):
        """`stated_reason` folds whitespace away, so a re-spelling there cannot
        change an observable — only a source guard keeps the fourth copy out."""
        source = (SCRIPTS / "roster_union.py").read_text()
        spellings = re.findall(r'or ""\)\.strip\(\)|isinstance\([^)]*, str\)'
                               r'\s+and\s+\w+\.strip\(\)|\.strip\(\) if isinstance',
                               source)
        self.assertEqual(len(spellings), 1,
                         f"blank-is-absent is spelled {len(spellings)}x, not once "
                         f"(only `declared` may spell it): {spellings}")

    def test_notify_reviewers_imports_the_predicate_rather_than_restating_it(self):
        source = (SCRIPTS / "notify_reviewers.py").read_text()
        self.assertRegex(source, r"from roster_union import [^\n]*\bdeclared\b")
        self.assertIn("declared(entry.get(key))", source)

    def test_states_field_asks_text_fields_for_text_and_others_for_a_value(self):
        for field in TEXT_FIELDS:
            for value in NON_STRINGS + BLANKS + (None,):
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.ru.states_field(field, value))
            self.assertTrue(self.ru.states_field(field, " x "))
        for value in NON_STRINGS:
            with self.subTest(field="allowlisted", value=value):
                self.assertTrue(self.ru.states_field("allowlisted", value))
        self.assertFalse(self.ru.states_field("allowlisted", None))

    def test_the_text_fields_are_named_once_and_both_readers_use_that_name(self):
        """A second literal list is how the union and the notifier come to
        disagree about which fields carry a reason at all."""
        self.assertEqual(self.ru.TEXT_FIELDS, TEXT_FIELDS)
        union_src = (SCRIPTS / "roster_union.py").read_text()
        notify_src = (SCRIPTS / "notify_reviewers.py").read_text()
        literal = re.compile(r'"refusal_basis"\s*,\s*"note"')
        self.assertEqual(len(literal.findall(union_src + notify_src)), 1,
                         "the text-field list is spelled more than once")
        # Identity is text too, and derived from IDENTITY_FIELDS rather than
        # re-spelled, so widening one cannot leave the other behind.
        self.assertIn("IDENTITY_FIELDS", union_src.split("TEXT_FIELDS =")[1][:200])
        for f in ("gh", "github", "same_actor_as", "authority_caveat"):
            self.assertIn(f, self.ru.TEXT_FIELDS)
        self.assertRegex(notify_src,
                         r"from roster_union import [^\n]*\bTEXT_FIELDS\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
