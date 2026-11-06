#!/usr/bin/env python3
"""The notifier's supported transports must agree with the shared classifier.

Two ways they disagreed, both reproduced against the production path rather than
against the union's helpers — a helper-level assertion passes while the consumer
that reads the helper still refuses the row it selected:

  1. The union counted a complete Discord row as usable and promoted a Discord
     PEER over a null local placeholder, shutting out a Matrix peer for the same
     person. `resolve()` cannot send on Discord, so the union's own winner was
     undeliverable and the reviewer was silently omitted.
  2. A route field holding a list or a dict passed the "names a route" test, so
     an unhashable value reached `resolve()`'s duplicate key and aborted the
     WHOLE batch; and a whitespace `stand` promoted by a refusal was returned as
     a live target while the classifier said the row had no route at all.

Six cases FAIL at a96e8312e58c7bcc9c7942a07eb408183d985387. Every one is paired
with a must-still-work leg in `StillWorks`, so a resolver that refused everything
— or a classifier tightened until nothing is a route — fails too.

The arms are written transport-agnostically wherever the invariant is, so they
keep their meaning once the tool learns a second transport; the one arm that
pins WHICH peer wins states that precondition and skips when it no longer holds.

Run: /usr/bin/python3 tests/notify-reviewers-transport-alignment.test.py
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest

SCRIPTS = (pathlib.Path(__file__).resolve().parents[1] / "skills"
           / "collaboration-intelligence" / "scripts")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Read once, defensively: a tree without the declaration must still COLLECT, or
# the arms that are meant to fail there never run at all.
DECLARED = tuple(getattr(_load("nr_decl", SCRIPTS / "notify_reviewers.py"),
                         "SUPPORTED_ROUTES", ()))

LOCAL_NULL = {"stand": None, "room": None, "human": "@r:ag2.space"}
PEER_DISCORD = {"discord_id": "123456789", "home_channel": "987654321"}
PEER_MATRIX = {"stand": "@peer:x", "room": "!peer:x"}
VALID = {"stand": "@valid:x", "room": "!valid:x"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ru = _load("ru_align", SCRIPTS / "roster_union.py")
        self.nr = _load("nr_align", SCRIPTS / "notify_reviewers.py")

    def _resolve(self, rosters, names):
        """Production `load_roster()` -> `resolve()`.

        Only on-disk DISCOVERY is supplied: the union call, the transport
        declaration it carries and the resolver all stay production, which is
        the seam a union-helper assertion cannot reach.
        """
        paths = []
        for label, rows in rosters:
            p = pathlib.Path(self.d, label + ".json")
            p.write_text(json.dumps(rows))
            paths.append((label, p))
        self.nr.roster_paths = lambda: paths
        roster = self.nr.load_roster()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            targets, rc = self.nr.resolve(list(names), roster)
        return targets, rc, err.getvalue(), roster


class UnionWinnerMustBeDeliverable(_Base):
    """Rosters ordered null local placeholder -> Discord peer -> Matrix peer."""

    ROSTERS = [("local", {"reviewer": LOCAL_NULL, "other": VALID}),
               ("host1", {"reviewer": PEER_DISCORD}),
               ("host2", {"reviewer": PEER_MATRIX})]

    def test_the_union_winner_is_one_resolve_can_actually_deliver(self):
        """Transport-agnostic, so it keeps its meaning once the tool learns a
        second transport: whatever row wins the bare key must be deliverable."""
        targets, rc, err, roster = self._resolve(self.ROSTERS, ("reviewer", "other"))
        self.assertIn(
            "reviewer", [t["name"] for t in targets],
            "the union handed resolve() a row it cannot send on; the reachable "
            f"row survives only under a suffix. rc={rc} keys={sorted(roster)} "
            f"err={err!r}")
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            set(self.ru.declared_routes(roster["reviewer"]))
            & set(self.nr.SUPPORTED_ROUTES),
            f"the winner declares {self.ru.declared_routes(roster['reviewer'])}, "
            f"none of {self.nr.SUPPORTED_ROUTES}")

    @unittest.skipUnless(DECLARED == ("matrix",),
                         "pins the selection while Matrix is the only transport")
    def test_the_MATRIX_peer_is_the_one_selected(self):
        targets, rc, err, _ = self._resolve(self.ROSTERS, ("reviewer",))
        self.assertEqual([(t["name"], t["stand"]) for t in targets],
                         [("reviewer", "@peer:x")], err)

    def test_a_LOCAL_row_this_tool_cannot_drive_is_not_the_end_of_it_either(self):
        """The same invariant with the undrivable route held LOCALLY: precedence
        by origin must not hand the resolver a row it will refuse."""
        targets, rc, err, roster = self._resolve(
            [("local", {"reviewer": {"discord_id": "D2", "home_channel": "C2"}}),
             ("zpeer", {"reviewer": PEER_MATRIX})], ("reviewer",))
        self.assertIn("reviewer", [t["name"] for t in targets],
                      f"rc={rc} keys={sorted(roster)} err={err!r}")
        self.assertEqual(rc, 0, err)

    def test_neither_peer_row_is_dropped(self):
        """The loser is kept under a suffix — a lost row and a row nobody wrote
        are indistinguishable afterwards."""
        _, _, _, roster = self._resolve(self.ROSTERS, ("reviewer",))
        rows = [v for k, v in roster.items() if k.split("@")[0] == "reviewer"]
        self.assertTrue(any(r.get("discord_id") for r in rows),
                        f"the Discord row was dropped: {sorted(roster)}")
        self.assertTrue(any(r.get("stand") == "@peer:x" for r in rows),
                        f"the Matrix row was dropped: {sorted(roster)}")

    def test_the_second_reviewer_resolves_either_way(self):
        """Must-still-work: the valid row never depended on the tie-break."""
        targets, _, err, _ = self._resolve(self.ROSTERS, ("reviewer", "other"))
        self.assertIn("other", [t["name"] for t in targets], err)


class MalformedRouteValues(_Base):
    """A bad row must be SKIPPED, never abort the batch around it."""

    def _batch(self, peer):
        return self._resolve([("local", {"bad": LOCAL_NULL, "good": VALID}),
                              ("host1", {"bad": peer})], ("bad", "good"))

    def test_a_LIST_route_does_not_crash_the_batch(self):
        targets, rc, err, _ = self._batch({"stand": ["@peer:x"], "room": "!peer:x"})
        self.assertEqual([t["name"] for t in targets], ["good"], err)
        self.assertEqual(rc, 3, err)

    def test_a_DICT_route_does_not_crash_the_batch(self):
        targets, rc, err, _ = self._batch(
            {"stand": {"mxid": "@peer:x"}, "room": "!peer:x"})
        self.assertEqual([t["name"] for t in targets], ["good"], err)
        self.assertEqual(rc, 3, err)

    def test_a_refusal_does_not_make_a_WHITESPACE_stand_a_target(self):
        """The refusal legitimately protects the row from promotion; it must not
        also make the blank it carries into a live mention target."""
        targets, rc, err, _ = self._batch(
            {"stand": "   ", "room": "!peer:x", "refusal_basis": "DO NOT ROUTE"})
        self.assertEqual([t["name"] for t in targets], ["good"],
                         f"a whitespace stand was returned as a target: {targets}")
        self.assertEqual(rc, 3, err)
        self.assertIn("DO NOT ROUTE", err,
                      "the refusal was dropped, so the obvious repair overrides it")

    def test_the_classifier_agrees_the_malformed_row_has_no_route(self):
        for value in (["@peer:x"], {"mxid": "@peer:x"}, "   ", "", None):
            self.assertEqual(
                self.ru.declared_routes({"stand": value, "room": "!peer:x"}), (),
                f"{value!r} was read as naming a Matrix route")


class StillWorks(_Base):
    """Controls: a blanket refusal, or a classifier tightened until nothing is a
    route, must fail this class."""

    def test_a_complete_matrix_entry_still_resolves(self):
        targets, rc, err, _ = self._resolve([("local", {"v": VALID})], ("v",))
        self.assertEqual(rc, 0, err)
        self.assertEqual([(t["name"], t["stand"], t["room"]) for t in targets],
                         [("v", "@valid:x", "!valid:x")])

    def test_an_off_allowlist_entry_still_refuses_with_its_OWN_code(self):
        """rc 4 must not collapse into the unusable rc 3."""
        _, rc, err, _ = self._resolve(
            [("local", {"v": dict(VALID, allowlisted=False)})], ("v",))
        self.assertEqual(rc, 4, err)

    def test_a_blank_stand_refusal_still_states_its_reason(self):
        _, rc, err, _ = self._resolve(
            [("local", {"v": {"stand": "", "room": "", "note": "DO NOT ROUTE"}})],
            ("v",))
        self.assertEqual(rc, 3, err)
        self.assertIn("DO NOT ROUTE", err)

    def test_an_unknown_reviewer_still_refuses_with_its_OWN_code(self):
        _, rc, err, _ = self._resolve([("local", {"v": VALID})], ("nobody",))
        self.assertEqual(rc, 2, err)

    def test_a_NUMERIC_discord_id_still_names_a_route(self):
        """Live rosters spell the id as a number; rejecting non-text wholesale
        would make this row unroutable to every consumer."""
        self.assertEqual(
            self.ru.declared_routes({"discord_id": 42, "home_channel": "C"}),
            ("discord",))
        self.assertEqual(
            self.ru.declared_routes({"stand_discord_id": 42, "home_channel": 7}),
            ("discord",))
        self.assertEqual(
            self.ru.declared_routes({"discord_id": True, "home_channel": "C"}), (),
            "a boolean is how a row says yes/no, never a snowflake")

    def test_a_route_value_is_delivered_STRIPPED(self):
        """The emitted target is the text the classifier validated: an unstripped
        copy addresses a different string and reads as a second person."""
        targets, rc, err, _ = self._resolve(
            [("local", {"v": {"stand": " @valid:x ", "room": " !valid:x "}})], ("v",))
        self.assertEqual(rc, 0, err)
        self.assertEqual([(t["stand"], t["room"]) for t in targets],
                         [("@valid:x", "!valid:x")])

    def test_the_DECLARATION_matches_what_resolve_actually_accepts(self):
        """The alignment itself, in both directions — so widening the resolver
        without widening the declaration keeps the union narrowing a row the
        resolver would now deliver, and fails here instead of silently."""
        for kind, row in (("matrix", VALID), ("discord", PEER_DISCORD)):
            targets, _, err, _ = self._resolve([("local", {"v": row})], ("v",))
            self.assertEqual(
                bool(targets), kind in self.nr.SUPPORTED_ROUTES,
                f"resolve() and SUPPORTED_ROUTES disagree about {kind}: "
                f"targets={targets} declared={self.nr.SUPPORTED_ROUTES} err={err!r}")

    def test_a_local_discord_route_still_wins_for_a_caller_that_can_send_on_it(self):
        """The default union is unchanged: narrowing is the CALLER's declaration,
        so the repair this PR made for a Discord-capable consumer still holds."""
        lp = pathlib.Path(self.d, "l.json")
        pp = pathlib.Path(self.d, "p.json")
        lp.write_text(json.dumps({"s": {"discord_id": "D2", "home_channel": "C2"}}))
        pp.write_text(json.dumps({"s": PEER_MATRIX}))
        u = self.ru.roster_union([("local", lp), ("zpeer", pp)])
        self.assertEqual(u["s"].get("discord_id"), "D2", sorted(u))
        self.assertIsNone(u["s"].get("stand"), u["s"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
