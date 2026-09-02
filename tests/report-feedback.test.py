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
        # Isolate from the real machine's ~/.sutando auth by pinning a fake home,
        # and from its Keychain by stubbing the keychain tier.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)
            bad = ws / "state" / "auth" / "cloud-auth.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("{not valid json")
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.object(report_feedback, "read_keychain_auth", return_value=(None, None)), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(report_feedback.read_cloud_auth(ws), (None, None))

    def test_dedupes_repeated_candidate_paths(self):
        # When the passed workspace IS the packaged-app workspace, two candidate
        # probe paths collide; the second must be skipped (seen-set continue).
        with tempfile.TemporaryDirectory() as fake_home:
            app_ws = Path(fake_home) / ".sutando" / "repo" / "workspace"
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.object(report_feedback, "read_keychain_auth", return_value=(None, None)), \
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
                    mock.patch.object(report_feedback, "read_keychain_auth", return_value=(None, None)), \
                    mock.patch.dict(os.environ, env, clear=True):
                base, token = report_feedback.read_cloud_auth(ws)
            self.assertEqual(token, "env-tok")
            self.assertEqual(base, "https://metered.example")

    def test_malformed_metering_env_is_swallowed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.object(report_feedback, "read_keychain_auth", return_value=(None, None)), \
                    mock.patch.dict(os.environ, {"SUTANDO_METERING_HEADERS": "{bad"}, clear=True):
                self.assertEqual(report_feedback.read_cloud_auth(ws), (None, None))

    def test_retired_apibase_in_auth_file_is_normalized(self):
        # A stale Electron-era file recording the retired .ai origin must not
        # steer the bearer into the redirect that drops it.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rec = ws / "state" / "auth" / "cloud-auth.json"
            rec.parent.mkdir(parents=True)
            rec.write_text(json.dumps({"apiBase": "https://sutando.ag2.ai", "token": "t"}))
            self.assertEqual(
                report_feedback.read_cloud_auth(ws),
                ("https://sutando.ag2.space", "t"),
            )

    def test_keychain_tier_used_when_no_auth_file(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fake_home:
            ws = Path(td)
            with mock.patch("pathlib.Path.home", return_value=Path(fake_home)), \
                    mock.patch.object(
                        report_feedback, "read_keychain_auth",
                        return_value=("https://sutando.ag2.space", "sutk_key"),
                    ), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    report_feedback.read_cloud_auth(ws),
                    ("https://sutando.ag2.space", "sutk_key"),
                )


class TestKeychainAuth(unittest.TestCase):
    # Exact key strings the host derives (cloud_session.rs origin_key_suffix):
    # a drifted slug or FNV hash silently orphans every stored session.
    def test_origin_vault_key_matches_host_derivation(self):
        self.assertEqual(
            report_feedback.origin_vault_key("https://sutando.ag2.space"),
            "AG2_CLOUD_TOKEN_HTTPS___SUTANDO_AG2_SPACE_F35E6C6AC0ABE4A4",
        )
        self.assertEqual(
            report_feedback.origin_vault_key("https://sutando.ag2.ai"),
            "AG2_CLOUD_TOKEN_HTTPS___SUTANDO_AG2_AI_9BCEA08BA2108268",
        )

    def test_signed_out_sentinel_reads_as_not_signed_in(self):
        with mock.patch.object(
            report_feedback, "_keychain_get", return_value=report_feedback.SIGNED_OUT_SENTINEL
        ), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(report_feedback.read_keychain_auth(), (None, None))

    def test_retired_origin_key_carries_over_to_default_origin(self):
        retired_key = report_feedback.origin_vault_key("https://sutando.ag2.ai")

        def keychain(key):
            return "sutk_old" if key == retired_key else None

        with mock.patch.object(report_feedback, "_keychain_get", side_effect=keychain), \
                mock.patch.dict(os.environ, {}, clear=True):
            # The base returned is the ACTIVE origin, never the retired one
            # (sending a bearer to the retired host drops it on the redirect).
            self.assertEqual(
                report_feedback.read_keychain_auth(),
                ("https://sutando.ag2.space", "sutk_old"),
            )

    def test_no_retired_fallback_for_non_default_origin(self):
        retired_key = report_feedback.origin_vault_key("https://sutando.ag2.ai")

        def keychain(key):
            return "sutk_old" if key == retired_key else None

        with mock.patch.object(report_feedback, "_keychain_get", side_effect=keychain), \
                mock.patch.dict(os.environ, {"AG2_CLOUD_ORIGIN": "http://localhost:3000"}, clear=True):
            self.assertEqual(report_feedback.read_keychain_auth(), (None, None))

    def test_keychain_get_reads_via_security_cli(self):
        ok = mock.Mock(returncode=0, stdout=b"sutk_tok\n")
        with mock.patch.object(report_feedback.sys, "platform", "darwin"), \
                mock.patch.object(report_feedback.subprocess, "run", return_value=ok) as run:
            self.assertEqual(report_feedback._keychain_get("K"), "sutk_tok")
        self.assertIn("find-generic-password", run.call_args.args[0])

    def test_keychain_get_absent_key_empty_value_and_error_read_as_none(self):
        missing = mock.Mock(returncode=44, stdout=b"")
        empty = mock.Mock(returncode=0, stdout=b"\n")
        with mock.patch.object(report_feedback.sys, "platform", "darwin"):
            with mock.patch.object(report_feedback.subprocess, "run", return_value=missing):
                self.assertIsNone(report_feedback._keychain_get("K"))
            with mock.patch.object(report_feedback.subprocess, "run", return_value=empty):
                self.assertIsNone(report_feedback._keychain_get("K"))
            with mock.patch.object(report_feedback.subprocess, "run", side_effect=OSError("no cli")):
                self.assertIsNone(report_feedback._keychain_get("K"))

    def test_keychain_get_is_darwin_only(self):
        with mock.patch.object(report_feedback.sys, "platform", "linux"), \
                mock.patch.object(report_feedback.subprocess, "run") as run:
            self.assertIsNone(report_feedback._keychain_get("K"))
        self.assertEqual(run.call_count, 0)

    def test_bare_legacy_key_is_last_resort(self):
        def keychain(key):
            return "sutk_bare" if key == "AG2_CLOUD_TOKEN" else None

        with mock.patch.object(report_feedback, "_keychain_get", side_effect=keychain), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                report_feedback.read_keychain_auth(),
                ("https://sutando.ag2.space", "sutk_bare"),
            )


class TestPrefs(unittest.TestCase):
    def test_missing_file_reports_but_sends_no_logs(self):
        # Absence is not consent: reporting still works, the excerpt does not.
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                report_feedback.read_prefs(Path(td)),
                {"autoReport": True, "sendLogs": False},
            )

    def test_reads_written_values(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            (ws / "state" / "feedback-prefs.json").write_text(
                json.dumps({"autoReport": False, "sendLogs": False})
            )
            self.assertEqual(
                report_feedback.read_prefs(ws),
                {"autoReport": False, "sendLogs": False},
            )

    def test_corrupt_or_nonbool_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            p = ws / "state" / "feedback-prefs.json"
            p.write_text("{not json")
            self.assertEqual(report_feedback.read_prefs(ws), {"autoReport": True, "sendLogs": False})
            # Each key falls back to its OWN default, and an explicit True for
            # sendLogs must still be honoured or the opt-in is unusable.
            p.write_text(json.dumps({"autoReport": "no", "sendLogs": True}))
            self.assertEqual(report_feedback.read_prefs(ws), {"autoReport": True, "sendLogs": True})


class TestAutoGate(unittest.TestCase):
    def test_first_report_passes_and_duplicate_is_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            now = 1_000_000.0
            ok, _ = report_feedback.check_auto_gate(ws, "Bridge crash", now)
            self.assertTrue(ok)
            report_feedback.record_auto_report(ws, "Bridge crash", now)
            ok, reason = report_feedback.check_auto_gate(ws, "bridge  CRASH", now + 60)
            self.assertFalse(ok, "same title (case/whitespace-insensitive) must dedupe")
            self.assertIn("identical", reason)
            ok, _ = report_feedback.check_auto_gate(ws, "Different bug", now + 60)
            self.assertTrue(ok)

    def test_dedupe_window_expires(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            now = 1_000_000.0
            report_feedback.record_auto_report(ws, "Bridge crash", now)
            ok, _ = report_feedback.check_auto_gate(
                ws, "Bridge crash", now + report_feedback.AUTO_DEDUPE_WINDOW_S + 1
            )
            self.assertTrue(ok)

    def test_daily_cap(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            now = 1_000_000.0
            for i in range(report_feedback.AUTO_DAILY_CAP):
                report_feedback.record_auto_report(ws, f"bug {i}", now + i)
            ok, reason = report_feedback.check_auto_gate(ws, "one more", now + 100)
            self.assertFalse(ok)
            self.assertIn("cap", reason)

    def test_record_failure_is_swallowed(self):
        # Throttle state is best-effort: an unwritable state dir must not
        # turn a filed report into a crash.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").write_text("a file where the dir belongs")
            report_feedback.record_auto_report(ws, "Bridge crash", 1.0)
            ok, _ = report_feedback.check_auto_gate(ws, "Bridge crash", 2.0)
            self.assertTrue(ok, "unreadable state reads as empty")

    def test_corrupt_state_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            (ws / "state" / report_feedback.AUTO_STATE_FILE).write_text("{bad")
            ok, _ = report_feedback.check_auto_gate(ws, "Bridge crash", 1.0)
            self.assertTrue(ok)


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

    def test_an_unlistable_directory_does_not_raise(self):
        """It runs on the failure path, so it must degrade, never raise.

        The sibling test below uses a directory named `bad.log`, which lets
        `iterdir()` succeed — so it exercises branch 3 and cannot catch this.
        An unreadable `logs/` would otherwise turn "filed without logs" into
        "not filed at all", for exactly the users whose logs are unreachable.
        """
        if os.geteuid() == 0:
            self.skipTest("root bypasses the permission bit")
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            (logs / "a.log").write_text("x\n")
            os.chmod(logs, 0o000)
            try:
                if os.access(logs, os.R_OK):
                    self.skipTest("filesystem does not enforce the permission bit")
                self.assertEqual(report_feedback.logs_excerpt(Path(td)), (None, []))
                why = report_feedback.why_no_logs(Path(td))
            finally:
                os.chmod(logs, 0o755)
        self.assertIn("could not be listed", why)

    def test_a_non_oserror_also_degrades(self):
        """The bare fallback is load-bearing, so it is covered rather than cut.

        `iterdir()` raising OSError was the obvious case, not the only one — and
        this function runs on the failure path, where raising would cost the
        report entirely. Covering the branch is the point; deleting it to satisfy
        a coverage gate would remove the guarantee.
        """
        class _Exploding:
            def __truediv__(self, other):
                return self

            def is_dir(self):
                raise RuntimeError("not an OSError")

            def __str__(self):
                return "<exploding>"

        why = report_feedback.why_no_logs(_Exploding())
        self.assertIn("could not be inspected", why)

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

    @staticmethod
    def _opt_in_to_logs(ws: Path) -> None:
        """sendLogs is opt-in, so a test ABOUT the excerpt must ask for it.

        These two cover the excerpt/omission-note logic, not the default. They
        used to rely on sendLogs defaulting ON, which made them silently depend
        on a policy they were not testing.
        """
        (ws / "state").mkdir(parents=True, exist_ok=True)
        (ws / "state" / "feedback-prefs.json").write_text(json.dumps({"sendLogs": True}))

    def test_absent_logs_are_explained_in_the_posted_context(self):
        """THE BUG: Odoo tickets carried exactly {source, platform, python}."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._opt_in_to_logs(ws)
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertNotIn("last_logs_excerpt", ctx)
        self.assertIn("logs_omitted", ctx)
        self.assertIn("no logs directory", ctx["logs_omitted"])

    def test_present_logs_carry_no_omission_note(self):
        """Mutation guard: the note must not fire when logs actually shipped."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._opt_in_to_logs(ws)
            (ws / "logs").mkdir()
            (ws / "logs" / "a.log").write_text("hello\n")
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertIn("last_logs_excerpt", ctx)
        self.assertNotIn("logs_omitted", ctx)

    def test_default_prefs_ship_no_logs_even_when_logs_exist(self):
        """The point of the change: no prefs file -> no excerpt, logs present."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "logs").mkdir()
            (ws / "logs" / "a.log").write_text("hello\n")
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertNotIn("last_logs_excerpt", ctx)
        self.assertNotIn("log_files", ctx)

    def test_opt_out_is_stated_so_triage_can_tell_it_from_missing_logs(self):
        # Silence is ambiguous: withheld consent and absent logs look identical.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "logs").mkdir()
            (ws / "logs" / "a.log").write_text("hello\n")
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertIs(ctx.get("logs_opted_out"), True)
        self.assertNotIn("logs_omitted", ctx)

    def test_opt_in_never_claims_an_opt_out(self):
        """Mutation guard: the marker must not fire when logs were requested."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._opt_in_to_logs(ws)
            (ws / "logs").mkdir()
            (ws / "logs" / "a.log").write_text("hello\n")
            ctx = self._posted_context(["--title", "hello"], ws)
        self.assertNotIn("logs_opted_out", ctx)
        self.assertIn("last_logs_excerpt", ctx)

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

    def test_auto_disabled_exits_3_without_posting(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            (ws / "state" / "feedback-prefs.json").write_text(json.dumps({"autoReport": False}))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen") as uo:
                with self.assertRaises(SystemExit) as cm:
                    self._run(["--title", "engine crash", "--auto"])
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(uo.call_count, 0)

    def test_auto_duplicate_exits_3_after_one_post(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()) as uo:
                self._run(["--title", "engine crash", "--auto", "--no-logs"])
                with self.assertRaises(SystemExit) as cm:
                    self._run(["--title", "engine crash", "--auto", "--no-logs"])
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(uo.call_count, 1, "the duplicate must not reach the API")

    def test_send_logs_pref_off_omits_logs_even_without_no_logs_flag(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "logs").mkdir()
            (ws / "logs" / "x.log").write_text("hello world\n")
            (ws / "state").mkdir()
            (ws / "state" / "feedback-prefs.json").write_text(json.dumps({"sendLogs": False}))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()) as uo:
                self._run(["--title", "hi"])
        payload = json.loads(uo.call_args.args[0].data.decode())
        self.assertNotIn("last_logs_excerpt", payload["context"])

    def test_auto_report_is_flagged_in_context(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", return_value=_FakeResp()) as uo:
                self._run(["--title", "engine crash", "--auto", "--no-logs"])
        payload = json.loads(uo.call_args.args[0].data.decode())
        self.assertIs(payload["context"]["auto"], True)

    def test_generic_error_exits_1(self):
        with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as cm:
                self._run(["--title", "hello", "--no-logs"])
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
