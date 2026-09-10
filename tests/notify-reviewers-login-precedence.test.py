#!/usr/bin/env python3
"""An explicit `same_actor_as` must outrank a roster key that merely looks like a login.

`_github_login` resolved the key first, so a roster key coinciding with an
unrelated real GitHub account routed the collaborator gate at that stranger.
Measured on a live roster: key `yixuan` is github.com/yixuan (a different
person, `read`), while the intended `yixuan-ag2` holds `write` — the gate
refused a legitimate reviewer. The dangerous polarity is the inverse: had the
colliding stranger held write, the gate would have PASSED and certified an
approver who is a different person.
"""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")

REAL_LOGINS = {"yixuan", "yixuan-ag2", "sonichi"}


def _load():
    spec = importlib.util.spec_from_file_location("_notify_reviewers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LoginPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.mod._is_github_user = lambda login: login in REAL_LOGINS

    def test_same_actor_as_wins_over_colliding_key(self):
        roster = {"yixuan": {"same_actor_as": "yixuan-ag2"}}
        login, why = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")
        self.assertIn("same_actor_as", why)

    def test_bare_key_still_resolves_when_no_mapping(self):
        login, why = self.mod._github_login("sonichi", {"sonichi": {}})
        self.assertEqual(login, "sonichi")
        self.assertEqual(why, "key is a login")

    def test_an_unresolvable_same_actor_as_is_KEPT_not_swapped_for_the_key(self):
        """Owner-stated, so it is not discarded on a probe that answers False.

        This arm previously asserted the opposite. The premise changed: falling
        back to the key is what routes the gate at a colliding stranger, and
        `_is_github_user` cannot tell "no such user" from "the probe failed".
        An unresolvable login reaches `gate_capability` and reports unverified.
        """
        roster = {"sonichi": {"same_actor_as": "not-a-real-account"}}
        login, why = self.mod._github_login("sonichi", roster)
        self.assertEqual(login, "not-a-real-account")
        self.assertIn("same_actor_as", why)

    def test_roster_gh_wins_over_a_colliding_key(self):
        # `gh` is the documented roster field and is a direct login, so it is
        # more explicit than a key alias resolved by probing.
        roster = {"yixuan": {"gh": "yixuan-ag2"}}
        login, why = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")
        self.assertIn("gh", why)

    def test_gh_outranks_same_actor_as(self):
        roster = {"yixuan": {"gh": "yixuan-ag2", "same_actor_as": "sonichi"}}
        login, _ = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")

    def test_an_unresolvable_gh_is_KEPT_not_swapped_for_the_key(self):
        roster = {"sonichi": {"gh": "no-such-account"}}
        login, why = self.mod._github_login("sonichi", roster)
        self.assertEqual(login, "no-such-account")
        self.assertEqual(why, "roster gh -> no-such-account")

    def test_the_github_spelling_resolves_exactly_like_gh(self):
        # Both spellings are deployed in ONE live roster file (5 `gh`, 2
        # `github`); the other reader of this store reads only `github`.
        roster = {"yixuan": {"github": "yixuan-ag2"}}
        login, why = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")
        self.assertIn("github", why)

    def test_an_unresolvable_github_is_KEPT_not_swapped_for_the_key(self):
        roster = {"sonichi": {"github": "no-such-account"}}
        login, why = self.mod._github_login("sonichi", roster)
        self.assertEqual(login, "no-such-account")
        self.assertEqual(why, "roster github -> no-such-account")

    def test_gh_wins_when_a_row_carries_BOTH_spellings(self):
        """Deterministic, so the two readers cannot pick different answers."""
        roster = {"yixuan": {"gh": "yixuan-ag2", "github": "sonichi"}}
        login, why = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")
        self.assertEqual(why, "roster gh -> yixuan-ag2")

    def test_an_EMPTY_identity_field_does_not_shadow_the_other_spelling(self):
        # A live row carries `gh: null`; `get("gh") or get("github")` must not
        # be re-derived per reader, and null must fall through, not dead-end.
        roster = {"yixuan": {"gh": None, "github": "yixuan-ag2"}}
        login, _ = self.mod._github_login("yixuan", roster)
        self.assertEqual(login, "yixuan-ag2")

    def test_a_MALFORMED_row_falls_through_instead_of_raising(self):
        # `(roster or {}).get(name) or {}` keeps a truthy non-dict, so a
        # hand-edited roster reaches the resolver as a string, not a mapping.
        login, why = self.mod._github_login("sonichi", {"sonichi": "yixuan-ag2"})
        self.assertEqual(login, "sonichi")
        self.assertEqual(why, "key is a login")

    def test_a_TRANSIENT_probe_failure_does_not_route_at_the_colliding_key(self):
        """The reviewer's repro: mapped login probes False, colliding key True.

        `_is_github_user` collapses timeout, nonzero rc and definitive absence
        into one False, so a network blip on the explicit login used to select
        the stranger — and if that stranger holds write, the gate certifies it.
        """
        calls = []

        def two_result_stub(login):
            calls.append(login)
            return login == "yixuan"      # the unrelated real account

        self.mod._is_github_user = two_result_stub
        login, why = self.mod._github_login("yixuan", {"yixuan": {"gh": "yixuan-ag2"}})
        self.assertEqual(login, "yixuan-ag2")
        self.assertIn("gh", why)

    def test_an_explicit_identity_is_never_PROBED(self):
        """Structural, not behavioural: no probe means no transient to lose to."""
        calls = []
        self.mod._is_github_user = lambda l: calls.append(l) or True
        self.mod._github_login("yixuan", {"yixuan": {"gh": "yixuan-ag2"}})
        self.mod._github_login("kewei", {"kewei": {"same_actor_as": "keweichen"}})
        self.assertEqual(calls, [], f"owner-stated identity was probed: {calls}")

    def test_neither_resolves(self):
        login, why = self.mod._github_login("stand-handle", {"stand-handle": {}})
        self.assertEqual(login, "stand-handle")
        self.assertEqual(why, "no login found for this key")


if __name__ == "__main__":
    unittest.main()
