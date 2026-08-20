#!/usr/bin/env python3
"""Regression tests for the report-feedback skill."""

import builtins
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "report-feedback" / "report-feedback.py"

spec = importlib.util.spec_from_file_location("report_feedback", SCRIPT)
report_feedback = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(report_feedback)


class TestReportFeedbackRedaction(unittest.TestCase):
    def test_redacts_aws_access_key(self):
        aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        redacted = report_feedback._redact(f"aws={aws_key}")

        self.assertNotIn(aws_key, redacted)
        self.assertIn("<redacted-token>", redacted)

    def test_redact_empty_string_is_noop(self):
        # Empty/falsy input returns unchanged without touching the regexes.
        self.assertEqual(report_feedback._redact(""), "")

    def test_redacts_slack_app_token(self):
        token = "xapp-" + "1-" + "A" * 12 + "-" + "B" * 12 + "-" + "C" * 20
        redacted = report_feedback._redact(f"slack app token {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("<redacted-token>", redacted)

    def test_redacts_google_api_key(self):
        key = "AIza" + "Sy" + "A" * 33
        redacted = report_feedback._redact(f"google api key {key}")

        self.assertNotIn(key, redacted)
        self.assertIn("<redacted-token>", redacted)

    def test_redacts_bare_query_key_without_consuming_next_param(self):
        redacted = report_feedback._redact("GET /v1beta/models?key=future-secret&alt=sse")

        self.assertIn("key=<redacted>&alt=sse", redacted)
        self.assertNotIn("future-secret", redacted)


class TestReportFeedbackCloudAuth(unittest.TestCase):
    def test_reads_migrated_workspace_auth_before_legacy_root(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            migrated = ws / "state" / "auth" / "cloud-auth.json"
            migrated.parent.mkdir(parents=True)
            migrated.write_text(
                json.dumps({"apiBase": "https://canonical.example", "token": "canonical-token"})
            )
            (ws / "cloud-auth.json").write_text(
                json.dumps({"apiBase": "https://legacy.example", "token": "legacy-token"})
            )

            self.assertEqual(
                report_feedback.read_cloud_auth(ws),
                ("https://canonical.example", "canonical-token"),
            )

    def test_skips_malformed_auth_file_and_returns_none_when_unsigned(self):
        # Invalid JSON in the canonical file must be swallowed (except: continue),
        # and with no other source and no metering env, the result is (None, None).
        # Isolate from the real machine's ~/.sutando auth by pinning a fake home.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)
            bad = ws / "state" / "auth" / "cloud-auth.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("{not valid json")
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(report_feedback.read_cloud_auth(ws), (None, None))

    def test_dedupes_repeated_candidate_paths(self):
        # When the passed workspace IS the packaged-app workspace, two candidate
        # probe paths collide; the second must be skipped (seen-set continue).
        with tempfile.TemporaryDirectory() as fake_home:
            app_ws = Path(fake_home) / ".sutando" / "repo" / "workspace"
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.dict(os.environ, {}, clear=True):
                # No files exist under the fake home, so this returns (None, None)
                # after exercising the dedup continue on the colliding path.
                self.assertEqual(report_feedback.read_cloud_auth(app_ws), (None, None))

    def test_reads_token_from_metering_env(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)  # no auth files present
            env = {
                "SUTANDO_METERING_HEADERS": json.dumps({"Authorization": "Bearer env-tok"}),
                "SUTANDO_METERING_ENDPOINT": "https://metered.example/api/usage/v2",
            }
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.dict(os.environ, env, clear=True):
                base, token = report_feedback.read_cloud_auth(ws)
            self.assertEqual(token, "env-tok")
            self.assertEqual(base, "https://metered.example")

    def test_malformed_metering_env_is_swallowed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.dict(os.environ, {"SUTANDO_METERING_HEADERS": "{bad"}, clear=True):
                self.assertEqual(report_feedback.read_cloud_auth(ws), (None, None))


class TestResolveWorkspace(unittest.TestCase):
    def test_returns_path(self):
        self.assertIsInstance(report_feedback.resolve_workspace(), Path)

    def test_falls_back_when_import_fails(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "workspace_default":
                raise ImportError("forced for test")
            return real_import(name, *args, **kwargs)

        sys.modules.pop("workspace_default", None)
        with mock.patch("builtins.__import__", side_effect=fake_import):
            ws = report_feedback.resolve_workspace()
        # Fallback is <repo>/workspace.
        self.assertEqual(ws.name, "workspace")


class TestLogsExcerpt(unittest.TestCase):
    def test_no_logs_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(report_feedback.logs_excerpt(Path(td)), (None, []))

    def test_reads_and_redacts_recent_logs(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            logs = ws / "logs"
            logs.mkdir()
            (logs / "a.log").write_text("token=supersecretvalue\nline2\n")
            excerpt, names = report_feedback.logs_excerpt(ws)
            self.assertIn("a.log", names)
            self.assertIn("<redacted>", excerpt)
            self.assertNotIn("supersecretvalue", excerpt)

    def test_unreadable_log_is_swallowed(self):
        # A ".log" that is actually a directory: it passes the suffix + stat
        # filter but read_text() raises → the except branch returns (None, []).
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            logs = ws / "logs"
            logs.mkdir()
            (logs / "bad.log").mkdir()
            self.assertEqual(report_feedback.logs_excerpt(ws), (None, []))


class TestWhyNoLogs(unittest.TestCase):
    """Logs are on by default, so their absence must be explained, not silent."""

    def test_names_the_missing_directory(self):
        """The reproduced case: a fresh checkout / worktree / CI runner.

        `workspace/*` is gitignored, so `<workspace>/logs` does not exist there
        and logs_excerpt() returns (None, []) — indistinguishable, before this,
        from a report that never asked for logs.
        """
        with tempfile.TemporaryDirectory() as td:
            why = report_feedback.why_no_logs(Path(td))
        self.assertIn("no logs directory", why)
        self.assertIn(str(Path(td) / "logs"), why,
                      "the reader needs the path that was looked at")

    def test_an_empty_directory_is_not_a_missing_one(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "logs").mkdir()
            why = report_feedback.why_no_logs(Path(td))
        self.assertIn("no .log files", why)
        self.assertNotIn("no logs directory", why)

    def test_present_but_unreadable_is_its_own_reason(self):
        """Pairs with test_unreadable_log_is_swallowed above: that one proves
        the excerpt is dropped, this one proves the drop is now explained."""
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            (logs / "bad.log").mkdir()
            why = report_feedback.why_no_logs(Path(td))
        self.assertIn("could not be read", why)


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestMain(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["report-feedback.py", *argv]):
            report_feedback.main()

    def test_blank_title_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(["--title", "   "])
        self.assertEqual(cm.exception.code, 1)

    def test_not_signed_in_exits_2(self):
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=(None, None)):
            with self.assertRaises(SystemExit) as cm:
                self._run(["--title", "hello"])
        self.assertEqual(cm.exception.code, 2)

    def test_successful_post_no_logs(self):
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()) as uo:
            self._run(["--title", "hello", "--no-logs"])
        self.assertEqual(uo.call_count, 1)

    def _posted_context(self, argv, ws):
        seen = {}

        def _capture(req, *a, **k):
            seen["ctx"] = json.loads(req.data.decode())["context"]
            return _FakeResp()

        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
            self._run(argv)
        return seen["ctx"]

    def test_absent_logs_are_explained_in_the_posted_context(self):
        """THE BUG: Odoo tickets carried exactly {source, platform, python}."""
        with tempfile.TemporaryDirectory() as td:
            ctx = self._posted_context(["--title", "hello"], Path(td))
        self.assertNotIn("last_logs_excerpt", ctx)
        self.assertIn("logs_omitted", ctx)
        self.assertIn("no logs directory", ctx["logs_omitted"])

    def test_present_logs_carry_no_omission_note(self):
        """Mutation guard: the note must not fire when logs actually shipped."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "logs").mkdir()
            (ws / "logs" / "a.log").write_text("hello\n")
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertIn("last_logs_excerpt", ctx)
        self.assertNotIn("logs_omitted", ctx)

    def test_no_logs_flag_is_not_an_omission_to_explain(self):
        """--no-logs is the user's choice, not a failure to report."""
        with tempfile.TemporaryDirectory() as td:
            ctx = self._posted_context(["--title", "hello", "--no-logs"], Path(td))
        self.assertNotIn("logs_omitted", ctx)
        self.assertNotIn("last_logs_excerpt", ctx)

    def test_successful_post_sets_explicit_user_agent(self):
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()) as uo:
            self._run(["--title", "hello", "--no-logs"])

        request = uo.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "Sutando-Feedback/1.0")

    def test_successful_post_attaches_logs(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "logs").mkdir()
            (ws / "logs" / "x.log").write_text("hello world\n")
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()):
                self._run(["--title", "hi", "--body", "detail", "--kind", "feature"])

    def test_http_error_exits_1(self):
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"invalid_payload"))  # type: ignore[arg-type]
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                self._run(["--title", "hello", "--no-logs"])
        self.assertEqual(cm.exception.code, 1)

    def test_generic_error_exits_1(self):
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as cm:
                self._run(["--title", "hello", "--no-logs"])
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
