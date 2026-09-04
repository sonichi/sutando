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

    def test_same_actor_as_to_a_nonlogin_falls_back_to_the_key(self):
        roster = {"sonichi": {"same_actor_as": "not-a-real-account"}}
        login, why = self.mod._github_login("sonichi", roster)
        self.assertEqual(login, "sonichi")
        self.assertEqual(why, "key is a login")

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

    def test_a_gh_that_is_not_a_login_falls_through(self):
        roster = {"sonichi": {"gh": "no-such-account"}}
        login, why = self.mod._github_login("sonichi", roster)
        self.assertEqual(login, "sonichi")
        self.assertEqual(why, "key is a login")

    def test_neither_resolves(self):
        login, why = self.mod._github_login("stand-handle", {"stand-handle": {}})
        self.assertEqual(login, "stand-handle")
        self.assertEqual(why, "no login found for this key")


if __name__ == "__main__":
    unittest.main()
