#!/usr/bin/env python3
"""A 64-hex digest is released only on verified public provenance (#6296).

Drives the real chat filter and the real Team result guard. The GitHub fetch is
the only stub: it stands in for the check run the guard would read itself.

Positive controls must stay held: a vendor token, an unverified 64-hex value,
a token under a digest label, a real 64-hex secret, and a verified digest whose
check run is not allowlisted, has another name, another author app, or does
not state that digest.

Run: python3 tests/egress-public-digest-provenance.test.py
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import secret_scanner
from chat_secret_filter import filter_chat_secrets
from policy.egress import public_digest
from policy.egress.result import TeamResultLeakError, scan_team_result

OWNER, NAME, CHECK = "example-org", "example-repo", "behavior-review-package"
SOURCES = ((f"{OWNER}/{NAME}", CHECK),)
RUN_ID = "424242"
RUN_URL = f"https://github.com/{OWNER}/{NAME}/runs/{RUN_ID}"
DIGEST = hashlib.sha256(b"public reproducible assessment fixture; not a credential").hexdigest()
OTHER_HEX = hashlib.sha256(b"an unrelated 64-hex value nobody published").hexdigest()
# Assembled at runtime so no literal token sits in the source tree.
VENDOR_TOKEN = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


def run_record(digest=DIGEST, name=CHECK, app="github-actions", owner=OWNER, repo=NAME):
    return {
        "name": name,
        "app": {"slug": app},
        "html_url": f"https://github.com/{owner}/{repo}/runs/{RUN_ID}",
        "output": {"summary": f"Assessment digest: `{digest}`", "text": ""},
    }


class Fetch:
    def __init__(self, record=None, error=None):
        self.record, self.error, self.calls = record or run_record(), error, []

    def __call__(self, path):
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.record


def review_body(value=DIGEST, cite=True, extra=""):
    block = json.dumps({"assessment_digest": value, "verdict": "review complete"}, indent=2)
    cited = f"Package check run: {RUN_URL}\n" if cite else ""
    return f"Review complete.\n{cited}```behavior-review\n{block}\n```\n{extra}"


def guard(body, fetch=None, sources=SOURCES):
    return scan_team_result(body, REPO, digest_sources=sources, digest_fetch=fetch or Fetch())


class FullScanner(unittest.TestCase):
    def test_detect_secrets_is_active(self):
        # Without the library the false positive this file pins cannot occur.
        self.assertTrue(secret_scanner.DETECT_SECRETS_ACTIVE)


class TheFalsePositive(unittest.TestCase):
    def test_unverified_digest_is_still_held_today(self):
        self.assertIn("Hex High Entropy String", filter_chat_secrets(review_body()).secret_types)
        with self.assertRaises(TeamResultLeakError):
            guard(review_body(), sources=())

    def test_verified_digest_is_delivered_unchanged(self):
        body = review_body()
        self.assertEqual(guard(body), body)

    def test_filter_clears_only_the_verified_digest_and_restores_it(self):
        body = review_body()
        result = filter_chat_secrets(body, public_digests=frozenset({DIGEST}))
        self.assertEqual(result.secret_types, ())
        self.assertEqual(result.text, body)

    def test_whole_line_bare_digest_is_cleared_through_the_same_helper(self):
        body = f"{RUN_URL}\n{DIGEST}\n"
        self.assertIn("Bare Hex Token", filter_chat_secrets(body).secret_types)
        self.assertEqual(guard(body), body)

    def test_job_url_form_is_accepted(self):
        body = review_body().replace(RUN_URL, f"https://github.com/{OWNER}/{NAME}/actions/runs/9/job/{RUN_ID}")
        self.assertEqual(guard(body), body)


class PositiveControls(unittest.TestCase):
    def assertHeld(self, body, fetch=None, sources=SOURCES):
        with self.assertRaises(TeamResultLeakError) as held:
            guard(body, fetch, sources)
        return str(held.exception)

    def test_vendor_token_beside_a_verified_digest(self):
        reason = self.assertHeld(review_body(extra=f"token: {VENDOR_TOKEN}\n"))
        self.assertIn("GitHub Token", reason)
        self.assertNotIn("Hex High Entropy String", reason)  # the digest itself was cleared

    def test_unverified_64_hex(self):
        self.assertHeld(review_body(value=OTHER_HEX))

    def test_token_under_a_misleading_digest_label(self):
        self.assertHeld(review_body(value=VENDOR_TOKEN))

    def test_real_64_hex_secret_beside_a_verified_digest(self):
        body = review_body(extra=json.dumps({"api_key": OTHER_HEX}) + "\n")
        self.assertIn("Hex High Entropy String", self.assertHeld(body))
        self.assertEqual(guard(review_body()), review_body())  # held for the secret alone

    def test_no_cited_run(self):
        fetch = Fetch()
        self.assertHeld(review_body(cite=False), fetch)
        self.assertEqual(fetch.calls, [])

    def test_repository_not_allowlisted(self):
        fetch = Fetch()
        self.assertHeld(review_body(), fetch, sources=(("someone/else", CHECK),))
        self.assertEqual(fetch.calls, [])

    def test_empty_allowlist_is_the_default_and_clears_nothing(self):
        self.assertEqual(public_digest.configured_sources(REPO), ())
        with self.assertRaises(TeamResultLeakError):
            scan_team_result(review_body(), REPO, digest_fetch=Fetch())

    def test_run_with_another_name(self):
        self.assertHeld(review_body(), Fetch(run_record(name="anything-else")))

    def test_run_not_created_by_github_actions(self):
        self.assertHeld(review_body(), Fetch(run_record(app="some-app")))

    def test_run_from_another_repository(self):
        self.assertHeld(review_body(), Fetch(run_record(repo="other-repo")))

    def test_run_that_published_a_different_digest(self):
        self.assertHeld(review_body(), Fetch(run_record(digest=OTHER_HEX)))

    def test_fetch_failure_fails_closed(self):
        self.assertHeld(review_body(), Fetch(error=OSError("offline")))


class Masking(unittest.TestCase):
    def test_digest_inside_a_longer_hex_run_is_not_masked(self):
        body = f'{{"k": "{DIGEST}{OTHER_HEX}"}}'
        masked, _ = public_digest.mask_digests(body, {DIGEST})
        self.assertEqual(masked, body)

    def test_preplanted_placeholder_disables_masking(self):
        body = f'{{"k": "{DIGEST}"}} verified-public-digest-0-end'
        masked, _ = public_digest.mask_digests(body, {DIGEST})
        self.assertEqual(masked, body)

    def test_clean_body_never_fetches(self):
        fetch = Fetch()
        self.assertEqual(guard(f"All good. {RUN_URL}", fetch), f"All good. {RUN_URL}")
        self.assertEqual(fetch.calls, [])


if __name__ == "__main__":
    unittest.main()
