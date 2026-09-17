#!/usr/bin/env python3
"""Regression test for the x-twitter browser login-completion sentinel.

The `login` command watches the on-disk cookie DB and treats sign-in as complete
once `authTokenOnDisk()` sees the authenticated marker. The original code counted
ANY of `auth_token`, `ct0`, `twid` — but `ct0` is a CSRF token X flushes for
guest/pre-auth sessions, so a `ct0`-only write false-positived login completion
and killed the window before the owner finished, leaving `check`/`post` to exit 2
(flaky login). Reported on PR #2133.

This test pulls the ACTUAL sentinel SQL out of x-post-browser.mjs (so it can't
drift from the code) and proves:
  - a profile with ONLY `ct0` → sentinel counts 0 → login keeps waiting
  - a profile with `auth_token` → sentinel counts 1 → login completes
  - the OLD 3-cookie predicate would have wrongly counted 1 for ct0-only
    (documents the exact false-positive the fix closes)

CI-safe: no Playwright, no network, no browser — just sqlite3 over fixtures.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MJS = os.path.join(REPO, "skills", "x-twitter", "x-post-browser.mjs")

# The buggy predicate the fix replaced — kept here only to prove the regression.
OLD_PREDICATE = ("name IN ('auth_token','ct0','twid') "
                 "AND (host_key='.x.com' OR host_key='x.com')")


def _sentinel_sql_from_source() -> str:
    """Extract the live sentinel query from authTokenOnDisk() in the .mjs."""
    with open(MJS, encoding="utf8") as f:
        src = f.read()
    # The SELECT COUNT(*) ... string literal passed to sqlite3.
    m = re.search(r'"(SELECT COUNT\(\*\) FROM cookies WHERE[^"]+)"', src)
    assert m, "could not find the sentinel SELECT in x-post-browser.mjs"
    return m.group(1)


def _seed(cookie_names):
    """Build a temp Cookies DB seeded with the given cookie names for x.com."""
    d = tempfile.mkdtemp(prefix="x-cookies-")
    db = os.path.join(d, "Cookies")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cookies (name TEXT, host_key TEXT);")
    con.executemany(
        "INSERT INTO cookies (name, host_key) VALUES (?, '.x.com');",
        [(n,) for n in cookie_names],
    )
    con.commit()
    con.close()
    return d, db


def _count(db, sql):
    return int(subprocess.check_output(["sqlite3", db, sql], text=True).strip() or 0)


class LoginSentinelTest(unittest.TestCase):
    def setUp(self):
        self.sql = _sentinel_sql_from_source()
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _fixture(self, names):
        d, db = _seed(names)
        self._dirs.append(d)
        return db

    def test_live_sentinel_targets_auth_token_only(self):
        # The shipped query must key on auth_token, not the 3-cookie set.
        self.assertIn("auth_token", self.sql)
        self.assertNotIn("ct0", self.sql,
                         "sentinel must not treat ct0 (CSRF) as an auth marker")
        self.assertNotIn("twid", self.sql)

    def test_ct0_only_keeps_login_waiting(self):
        db = self._fixture(["ct0"])
        self.assertEqual(_count(db, self.sql), 0,
                         "ct0-only profile must NOT satisfy the login sentinel")

    def test_auth_token_completes_login(self):
        db = self._fixture(["auth_token", "ct0", "twid"])
        self.assertGreaterEqual(_count(db, self.sql), 1,
                                "authenticated profile must satisfy the sentinel")

    def test_old_predicate_would_have_false_positived(self):
        # Guard: prove the bug was real — the old 3-cookie predicate counts the
        # ct0-only profile as signed-in.
        db = self._fixture(["ct0"])
        old_sql = f"SELECT COUNT(*) FROM cookies WHERE {OLD_PREDICATE};"
        self.assertGreaterEqual(_count(db, old_sql), 1)
        # ...and the fixed sentinel does not.
        self.assertEqual(_count(db, self.sql), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
