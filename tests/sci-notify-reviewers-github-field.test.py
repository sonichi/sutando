#!/usr/bin/env python3
"""_github_login must read the entry's `github` field before guessing from the key.

A roster key can collide with a real, unrelated GitHub account: `rui` (key) and
`rui` (a stranger's login) both exist, so the key heuristic silently probes the
wrong person and reports a confident capability verdict about someone else.
"""
import importlib.util
import sys
import unittest
import unittest.mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "skills/collaboration-intelligence/scripts/notify_reviewers.py"
_spec = importlib.util.spec_from_file_location("nr_gh", _SRC)
nr = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(nr)
except SystemExit:
    pass


class GithubFieldOutranksTheKey(unittest.TestCase):
    def setUp(self):
        # Every key looks like a real account, which is exactly the collision.
        self._p = unittest.mock.patch.object(nr, "_is_github_user", return_value=True)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_the_stated_field_wins_over_a_colliding_key(self):
        roster = {"rui": {"github": "john-the-dev"}}
        login, why = nr._github_login("rui", roster)
        self.assertEqual(login, "john-the-dev",
                         "the key collided with a real stranger's login and won")
        self.assertIn("github", why)

    def test_the_owner_alias_resolves_too(self):
        roster = {"chi-wang": {"github": "sonichi"}}
        self.assertEqual(nr._github_login("chi-wang", roster)[0], "sonichi")

    def test_a_key_that_equals_its_login_is_unchanged(self):
        """Negative control: the fix must not perturb the ordinary case."""
        roster = {"keweichen": {"github": "keweichen"}}
        self.assertEqual(nr._github_login("keweichen", roster)[0], "keweichen")

    def test_no_github_field_still_falls_back_to_the_key(self):
        """Negative control: entries without the field keep the old behaviour."""
        roster = {"someone": {"stand": "@s:x"}}
        login, why = nr._github_login("someone", roster)
        self.assertEqual(login, "someone")
        self.assertEqual(why, "key is a login")

    def test_an_absent_entry_does_not_raise(self):
        self.assertEqual(nr._github_login("ghost", {})[0], "ghost")


if __name__ == "__main__":
    unittest.main(verbosity=2)
