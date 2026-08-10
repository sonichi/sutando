#!/usr/bin/env python3
"""Unit tests for the headless PostHog usage reporter.

All vault and network access is mocked. These tests deliberately exercise the
operator-visible unavailable messages and report shape without reading a real
credential or querying PostHog.
"""
import importlib.util
import io
import subprocess
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import call, patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "posthog-usage-report.py"


def _load():
    spec = importlib.util.spec_from_file_location("posthog_usage_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(
        args=["secret-vault.py"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestVaultLookup(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_repo_root_finds_checkout(self):
        self.assertEqual(self.mod._repo_root(), REPO)

    def test_no_repo_or_vault_key_returns_none(self):
        with patch.object(self.mod, "_repo_root", return_value=None), \
             patch.object(self.mod.subprocess, "run") as run:
            self.assertIsNone(self.mod._vault_key())
        run.assert_not_called()

    def test_primary_vault_key_wins(self):
        with patch.object(self.mod, "_repo_root", return_value=REPO), \
             patch.object(
                 self.mod.subprocess, "run", return_value=_completed("primary\n")
             ) as run:
            self.assertEqual(self.mod._vault_key(), "primary")
        self.assertIn("POSTHOG_PERSONAL_APIKEY", run.call_args.args[0])
        self.assertEqual(run.call_count, 1)

    def test_falls_back_to_alternate_vault_key_after_failure(self):
        with patch.object(self.mod, "_repo_root", return_value=REPO), \
             patch.object(
                 self.mod.subprocess,
                 "run",
                 side_effect=[OSError("vault unavailable"), _completed("fallback\n")],
             ) as run:
            self.assertEqual(self.mod._vault_key(), "fallback")
        self.assertEqual(run.call_count, 2)
        self.assertIn("POSTHOG_PERSONAL_API_KEY", run.call_args.args[0])

    def test_nonzero_and_empty_vault_results_are_ignored(self):
        with patch.object(self.mod, "_repo_root", return_value=REPO), \
             patch.object(
                 self.mod.subprocess,
                 "run",
                 side_effect=[_completed(returncode=1), _completed("  ")],
             ):
            self.assertIsNone(self.mod._vault_key())


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_query_builds_authenticated_hogql_request(self):
        response = io.BytesIO(b'{"results":[["ok"]]}')
        with patch.object(
            self.mod.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(self.mod._q("secret", "select 1"), [["ok"]])

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, self.mod.API)
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 45)
        self.assertIn(b'"query": "select 1"', request.data)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_missing_key_is_unavailable(self):
        with patch.object(self.mod, "_vault_key", return_value=None), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.mod.main(), 1)
        self.assertEqual(
            stdout.getvalue(),
            "unavailable: no POSTHOG_PERSONAL_APIKEY in vault\n",
        )

    def test_http_error_is_unavailable(self):
        error = urllib.error.HTTPError(
            self.mod.API,
            403,
            "Forbidden",
            {},
            io.BytesIO(b"denied by PostHog"),
        )
        with patch.object(self.mod, "_vault_key", return_value="secret"), \
             patch.object(self.mod, "_q", side_effect=error), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.mod.main(), 1)
        self.assertEqual(
            stdout.getvalue(),
            "unavailable: PostHog API 403 — denied by PostHog\n",
        )

    def test_generic_error_is_unavailable(self):
        with patch.object(self.mod, "_vault_key", return_value="secret"), \
             patch.object(self.mod, "_q", side_effect=TimeoutError("timed out")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.mod.main(), 1)
        self.assertEqual(stdout.getvalue(), "unavailable: timed out\n")

    def test_happy_path_formats_complete_report(self):
        query_results = [
            [[42]],
            [[7]],
            [["2026-07-27", 3], ["2026-07-28", 4]],
            [["2026-07-27", 1]],
            [[5]],
            [["task_processed", 12, 4], ["core_started", 7, 3]],
        ]
        with patch.object(self.mod, "_vault_key", return_value="secret"), \
             patch.object(self.mod, "_q", side_effect=query_results) as query, \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.mod.main(), 0)

        self.assertEqual(query.call_count, 6)
        self.assertEqual(query.call_args_list[0], call("secret", unittest.mock.ANY))
        output = stdout.getvalue()
        self.assertIn("Sutando usage (PostHog 504955, headless)", output)
        self.assertIn("churn-inflated upper bound): 42", output)
        self.assertIn("- WAU (7d distinct persons): 7", output)
        self.assertIn("- returning in last 24h (seen before): 5", output)
        self.assertIn("2026-07-28: 4", output)
        self.assertIn("task_processed", output)
        self.assertIn("core_started", output)


if __name__ == "__main__":
    unittest.main()
