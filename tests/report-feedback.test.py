#!/usr/bin/env python3
"""Regression tests for the report-feedback skill."""

import builtins
import importlib.util
import contextlib
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
                {"autoReport": True, "sendLogs": False, "askFirst": False},
            )

    def test_reads_written_values(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            (ws / "state" / "feedback-prefs.json").write_text(
                json.dumps({"autoReport": False, "sendLogs": False, "askFirst": False})
            )
            self.assertEqual(
                report_feedback.read_prefs(ws),
                {"autoReport": False, "sendLogs": False, "askFirst": False},
            )

    def test_corrupt_or_nonbool_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            p = ws / "state" / "feedback-prefs.json"
            p.write_text("{not json")
            self.assertEqual(report_feedback.read_prefs(ws), {"autoReport": True, "sendLogs": False, "askFirst": False})
            # Each key falls back to its OWN default, and an explicit True for
            # sendLogs must still be honoured or the opt-in is unusable.
            p.write_text(json.dumps({"autoReport": "no", "sendLogs": True}))
            self.assertEqual(report_feedback.read_prefs(ws), {"autoReport": True, "sendLogs": True, "askFirst": False})


class TestAskFirst(unittest.TestCase):
    """Ask-first parks the report and registers its card in the HITL store; the click files or drops it."""

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["report-feedback.py", *argv]):
            report_feedback.main()

    def _ws(self, td, prefs=None):
        ws = Path(td); (ws / "state").mkdir()
        if prefs is not None:
            (ws / "state" / "feedback-prefs.json").write_text(json.dumps(prefs))
        return ws

    def test_decide_skip_drops_the_draft_without_filing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "medium", "title": "t", "body": "b", "auto": True})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", side_effect=AssertionError("must not file")):
                self._run(["--decide", did, "skip"])  # the documented form: no --title
            self.assertEqual(report_feedback.list_drafts(ws), [])

    def test_decide_file_posts_the_parked_report_and_records_it(self):
        posted = []
        def _capture(req, timeout=None):
            posted.append((req.full_url, json.loads(req.data.decode()))); return _FakeResp()
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"sendLogs": False})
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "high", "title": "relay down", "body": "details", "auto": True})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                self._run(["--decide", did, "file"])
            self.assertEqual(posted[0][0], "https://x/api/feedback")
            body = posted[0][1]
            self.assertEqual((body["title"], body["severity"], body["context"]["owner_approved"]), ("relay down", "high", True))
            self.assertTrue(body["context"]["logs_opted_out"])
            self.assertEqual(report_feedback.list_drafts(ws), [])

    def test_drafts_runs_without_a_title_and_lists_what_is_parked(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b"})
            buf = io.StringIO()
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    contextlib.redirect_stdout(buf):
                self._run(["--drafts"])
            self.assertEqual([d["id"] for d in json.loads(buf.getvalue())], [did])

    def test_filing_still_requires_a_title(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    self.assertRaises(SystemExit) as cm:
                self._run(["--body", "no title given"])
            self.assertEqual(cm.exception.code, 1)

    def test_draft_store_tolerates_junk_and_absence(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b"})
            (report_feedback._drafts_dir(ws) / "fb_corrupt.json").write_text("{not json")
            self.assertEqual([d["id"] for d in report_feedback.list_drafts(ws)], [did])
            self.assertIsNone(report_feedback.load_draft(ws, "fb_0123456789"))
            report_feedback.drop_draft(ws, "fb_0123456789")  # absent is the dropped state, not an error

    def test_decide_refuses_a_bad_choice_a_missing_draft_and_a_signed_out_host(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b", "auto": True})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                with self.assertRaises(SystemExit) as cm:
                    self._run(["--decide", did, "maybe"])
                self.assertEqual(cm.exception.code, 1)
                with self.assertRaises(SystemExit) as cm:
                    self._run(["--decide", "fb_nope", "file"])
                self.assertEqual(cm.exception.code, 1)
                with mock.patch.object(report_feedback, "read_cloud_auth", return_value=(None, None)):
                    with self.assertRaises(SystemExit) as cm:
                        self._run(["--decide", did, "file"])
                    self.assertEqual(cm.exception.code, 2)
            self.assertEqual(len(report_feedback.list_drafts(ws)), 1, "a refused decision keeps the draft parked")

    def test_decide_file_attaches_logs_when_allowed_and_explains_their_absence(self):
        posted = []
        def _capture(req, timeout=None):
            posted.append(json.loads(req.data.decode())); return _FakeResp()
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"sendLogs": True})
            d1 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "with logs", "body": "b", "auto": False})
            d2 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "no logs on disk", "body": "b", "auto": False})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                with mock.patch.object(report_feedback, "logs_excerpt", return_value=("tail of log", ["a.log"])):
                    self._run(["--decide", d1, "file"])
                with mock.patch.object(report_feedback, "logs_excerpt", return_value=("", [])), \
                        mock.patch.object(report_feedback, "why_no_logs", return_value="no logs dir"):
                    self._run(["--decide", d2, "file"])
            self.assertEqual((posted[0]["context"]["last_logs_excerpt"], posted[0]["context"]["log_files"]), ("tail of log", ["a.log"]))
            self.assertEqual(posted[1]["context"]["logs_omitted"], "no logs dir")
            self.assertEqual(report_feedback.list_drafts(ws), [])

    def test_decide_file_reports_api_and_transport_errors_and_keeps_the_draft(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b", "auto": True})
            http_err = urllib.error.HTTPError("https://x/api/feedback", 500, "boom", {}, io.BytesIO(b"server said no"))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")):
                with mock.patch.object(report_feedback, "post_feedback", side_effect=http_err):
                    with self.assertRaises(SystemExit) as cm:
                        self._run(["--decide", did, "file"])
                    self.assertEqual(cm.exception.code, 1)
                with mock.patch.object(report_feedback, "post_feedback", side_effect=OSError("offline")):
                    with self.assertRaises(SystemExit) as cm:
                        self._run(["--decide", did, "file"])
                    self.assertEqual(cm.exception.code, 1)
            # a 5xx and a transport error both leave the draft in flight: held for the owner, not a
            # plain draft a retry would post again
            self.assertEqual(report_feedback.list_drafts(ws), [])
            self.assertEqual([d["id"] for d in report_feedback.list_drafts(ws, state="posting")], [did])

    def _hitl(self, ws):
        return report_feedback.hitl_manager(ws)

    def test_auto_with_ask_first_registers_a_card_in_the_hitl_store_and_files_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"askFirst": True, "autoReport": True})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.dict(os.environ, {"REMOTE_TASK_URL": "", "REMOTE_TASK_TOKEN": ""}), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=AssertionError("nothing is posted")), \
                    mock.patch.object(report_feedback, "read_cloud_auth", side_effect=AssertionError("must not file")):
                self._run(["--title", "gateway dropped the relay", "--auto"])
            drafts = report_feedback.list_drafts(ws)
            self.assertEqual(len(drafts), 1)
            reqs = self._hitl(ws).active()
            self.assertEqual(len(reqs), 1)
            req = reqs[0]
            self.assertEqual((req.runtime, req.kind, req.guard, req.status), ("report-feedback", "choice", drafts[0]["id"], "pending"))
            self.assertEqual([a.id for a in req.actions], ["file", "file_no_logs", "skip"])
            self.assertEqual(req.subject["draft_id"], drafts[0]["id"])
            # the ask is the throttled event: the ledger is written now, not at filing
            self.assertFalse(report_feedback.check_auto_gate(ws, "gateway dropped the relay")[0])
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    self.assertRaises(SystemExit) as cm:
                self._run(["--title", "gateway dropped the relay", "--auto"])
            self.assertEqual(cm.exception.code, 3, "an identical ask is deduped")
            self.assertEqual(len(self._hitl(ws).active()), 1, "no second card")

    def test_bare_ask_honours_the_off_switch_and_the_cap(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"autoReport": False})
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    self.assertRaises(SystemExit) as cm:
                self._run(["--title", "x", "--ask"])
            self.assertEqual(cm.exception.code, 3)
            self.assertEqual(report_feedback.list_drafts(ws), [])
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            for i in range(report_feedback.AUTO_DAILY_CAP):
                report_feedback.record_auto_report(ws, f"bug {i}")
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    self.assertRaises(SystemExit) as cm:
                self._run(["--title", "one more", "--ask"])
            self.assertEqual(cm.exception.code, 3)
            self.assertEqual(report_feedback.list_drafts(ws), [], "a capped ask parks nothing")

    def test_a_failed_registration_keeps_the_draft_and_apply_retries_it(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "register_ask", side_effect=OSError("store locked")), \
                    self.assertRaises(SystemExit) as cm:
                self._run(["--title", "kept", "--ask"])
            self.assertEqual(cm.exception.code, 3, "retryable, not an error")
            self.assertEqual(len(report_feedback.list_drafts(ws)), 1, "the report is not lost")
            self.assertEqual(self._hitl(ws).active(), [])
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--apply"])
            self.assertEqual(len(self._hitl(ws).active()), 1, "--apply registered the parked draft")

    def test_draft_ids_outside_the_grammar_never_become_paths(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            victim = ws / "victim.json"
            victim.write_text(json.dumps({"id": "x", "payload": {"title": "t"}}))
            # The drafts dir must exist for `..` to resolve through it: without it every path fails
            # ENOENT and the traversal is masked, not refused (a control that cannot go red).
            good = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b"})
            for bad in ("../../victim", "../victim", "fb_../victim", "fb_ABCDEF0123", "fb_short", "", "hitl_deadbeef00"):
                self.assertIsNone(report_feedback.load_draft(ws, bad), bad)
                self.assertEqual(report_feedback.decide(ws, {}, bad, "skip"), 1, bad)
                with self.assertRaises(ValueError):
                    report_feedback.drop_draft(ws, bad)
            self.assertTrue(victim.exists(), "a traversal id must not read or unlink outside the drafts dir")
            self.assertRegex(good, r"^fb_[0-9a-f]{10}$")
            self.assertEqual(report_feedback.decide(ws, {}, good, "skip"), 0)

    def test_a_relay_click_is_applied_by_the_bridge_and_apply_files_the_draft(self):
        """The real round trip: ask → requirement in the store → the bridge's task-relay click
        handler applies the click → --apply files the parked report and resolves the card."""
        import importlib
        for pth in (str(Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"),):
            if pth not in sys.path:
                sys.path.insert(0, pth)
        posted = []
        def _capture(req, timeout=None):
            posted.append((req.full_url, json.loads(req.data.decode()))); return _FakeResp()
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"askFirst": True, "sendLogs": False})
            (ws / "tasks").mkdir(); (ws / "results").mkdir()
            from ag2_sparrow._dirs import set_dirs
            set_dirs(task_dir=ws / "tasks", result_dir=ws / "results", state_dir=ws / "state")
            rgb = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
            owner = "@owner:ag2.space"
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.dict(os.environ, {"SPARROW_HA_OWNER": owner}):
                self._run(["--title", "relay down", "--body", "details", "--severity", "high", "--auto"])
                req = self._hitl(ws).active()[0]
                click = {"id": "task-click1", "channel_id": "!dm:ag2.space", "user_id": owner, "source_message_id": "$c",
                         "task": "File this bug report",
                         "hitl_action": {"hitl_id": req.id, "expected_revision": req.revision, "action_id": "file", "guard": req.guard}}
                with mock.patch.object(rgb, "_STATE", ws / "state"):
                    out = rgb._handle_hitl_action(click)
                # False = the bridge keeps the click on the task path: the core takes a turn on it,
                # and the turn's Stop hook is what files the draft. Nobody types --apply.
                self.assertIs(out, False)
                after = self._hitl(ws).get(req.id)
                self.assertEqual((after.status, after.chosen_action), ("in_progress", "file"))
                # a stranger's click on the same card is ignored and changes nothing
                stranger = dict(click, user_id="@someone:ag2.space", id="task-click2")
                with mock.patch.object(rgb, "_STATE", ws / "state"):
                    self.assertEqual(rgb._handle_hitl_action(stranger), "ignored")
                import importlib.util
                spec = importlib.util.spec_from_file_location("apply_clicks_hook_rt", Path(__file__).resolve().parent.parent / "skills" / "report-feedback" / "hooks" / "apply-clicks.py")
                hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
                with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                        mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                    self.assertEqual(hook.main(workspace=ws, rf=report_feedback), 0)
            self.assertEqual(posted[0][0], "https://x/api/feedback")
            body = posted[0][1]
            self.assertEqual((body["title"], body["severity"], body["context"]["owner_approved"]), ("relay down", "high", True))
            self.assertEqual(report_feedback.list_drafts(ws), [])
            self.assertEqual(self._hitl(ws).get(req.id).status, "resolved")
            self.assertEqual(len(report_feedback._read_auto_state(ws)), 1, "asked once, filed once: one ledger entry")

    def test_apply_keeps_a_click_it_cannot_complete_and_cancels_one_whose_draft_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b", "auto": True})
            m = self._hitl(ws)
            rid = report_feedback.register_ask(ws, did, "t", "host")
            from hitl.schema import ActionReply
            m.apply_action(ActionReply(hitl_id=rid, expected_revision=1, action_id="file", guard=did))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=(None, None)):
                self._run(["--apply"])
            self.assertEqual(m.get(rid).status, "in_progress", "signed out: the click waits")
            self.assertEqual(len(report_feedback.list_drafts(ws)), 1)
            report_feedback.drop_draft(ws, did)  # decided by hand meanwhile
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--apply"])
            self.assertEqual(m.get(rid).status, "cancelled")

    def test_decide_by_hand_resolves_the_card(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b", "auto": True})
            rid = report_feedback.register_ask(ws, did, "t", "host")
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--decide", did, "skip"])  # a clean decision returns; only failures exit
            self.assertEqual(self._hitl(ws).get(rid).status, "resolved")
            self.assertEqual(report_feedback.list_drafts(ws), [])

    def test_a_clean_process_asks_and_registers_the_card(self):
        """Codex's control: a fresh interpreter, nothing imported before the ask. At 68e48b61 the
        schema import ran before the engine path was set and every first ask parked with exit 3."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "skills" / "report-feedback" / "report-feedback.py"
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"askFirst": True})
            code = (
                "import importlib.util, sys; from pathlib import Path\n"
                f"spec = importlib.util.spec_from_file_location('rf', {str(script)!r}); rf = importlib.util.module_from_spec(spec); spec.loader.exec_module(rf)\n"
                f"rf.resolve_workspace = lambda: Path({td!r})\n"
                "sys.argv = ['report-feedback.py', '--title', 'clean run', '--auto']; rf.main()"
            )
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ASKED:", r.stdout)
            self.assertEqual(len(self._hitl(ws).active()), 1, "the card exists after one clean ask")

    def test_a_retry_after_the_post_landed_does_not_post_again(self):
        """Receipt transition: post → rename the draft to its receipt → resolve. A failure after the
        post leaves the receipt; the retry closes the card from it and posts nothing."""
        posted = []
        def _capture(req, timeout=None):
            posted.append(json.loads(req.data.decode())); return _FakeResp()
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"sendLogs": False})
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "once", "body": "b", "auto": True})
            m = self._hitl(ws)
            rid = report_feedback.register_ask(ws, did, "once", "host")
            from hitl.schema import ActionReply
            from hitl.manager import HitlManager
            m.apply_action(ActionReply(hitl_id=rid, expected_revision=1, action_id="file", guard=did))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                with mock.patch.object(HitlManager, "resolve", side_effect=OSError("store lock lost")):
                    with self.assertRaises(OSError):
                        self._run(["--apply"])
                self.assertEqual(len(posted), 1)
                self.assertTrue(report_feedback.filed_receipt(ws, did).exists(), "the receipt survives the failure")
                self.assertEqual(m.get(rid).status, "in_progress")
                self._run(["--apply"])
            self.assertEqual(len(posted), 1, "the retry must not post a second report")
            self.assertEqual(m.get(rid).status, "resolved")
            self.assertFalse(report_feedback.filed_receipt(ws, did).exists())
            self.assertEqual(report_feedback.list_drafts(ws), [])
            self.assertEqual(posted[0]["context"]["idempotency_key"], did)

    def test_the_stop_hook_applies_a_click_without_anyone_running_apply(self):
        """The skill-owned executor: the manifest declares a Stop hook, discovery sees it, and the
        hook's entry point finishes an answered card with nobody typing --apply. A hook must never
        raise (it would block the agent), so a broken store is swallowed and retried next turn."""
        import importlib.util
        repo = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo / "src"))
        from skill_hooks import discover
        hooks = [r for r in discover(repo) if r[1] == "apply-clicks.py"]
        self.assertEqual([h[0] for h in hooks], ["Stop"])
        spec = importlib.util.spec_from_file_location("apply_clicks_hook", repo / "skills" / "report-feedback" / "hooks" / "apply-clicks.py")
        h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
        self.assertTrue(hasattr(h.load_rf(), "apply_clicks"), "the hook loads the sibling script from disk")
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            self.assertEqual(h.main(workspace=ws, rf=report_feedback), 0, "nothing pending: a no-op")
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b", "auto": True})
            m = self._hitl(ws)
            rid = report_feedback.register_ask(ws, did, "t", "host")
            from hitl.schema import ActionReply
            m.apply_action(ActionReply(hitl_id=rid, expected_revision=1, action_id="skip", guard=did))
            self.assertEqual(report_feedback.pending_clicks(ws), 1)
            self.assertEqual(h.main(workspace=ws, rf=report_feedback), 0)
            self.assertEqual(m.get(rid).status, "resolved", "the hook finished the click")
            self.assertEqual(report_feedback.list_drafts(ws), [])
            self.assertEqual(report_feedback.pending_clicks(ws), 0)
            with mock.patch.object(report_feedback, "apply_clicks", side_effect=RuntimeError("store gone")):
                m.create(report_feedback.hitl_manager(ws).get(rid).__class__(kind="choice", runtime="report-feedback", message="x", guard="fb_0000000000", chosen_action="skip", status="in_progress"))
                self.assertEqual(h.main(workspace=ws, rf=report_feedback), 0, "a raising apply never escapes the hook")

    def test_apply_cancels_a_card_with_a_foreign_guard_and_a_receipt_is_a_finished_filing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            m = self._hitl(ws)
            from hitl.schema import Action, ActionReply, HumanRequirement
            # A requirement in this skill's runtime whose guard is not a draft id can only be foreign or forged.
            odd = m.create(HumanRequirement(kind="choice", runtime="report-feedback", message="x", guard="../../etc",
                                            device={"id": "report-feedback:odd"},
                                            actions=[Action(id="skip", kind="confirmation", label="Skip")]))
            m.apply_action(ActionReply(hitl_id=odd.id, expected_revision=1, action_id="skip", guard="../../etc"))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--apply"])
            self.assertEqual(m.get(odd.id).status, "cancelled")
            # A receipt means the post landed: deciding again is a no-op success, never a second post.
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "t", "body": "b"})
            report_feedback.mark_posting(ws, did)
            report_feedback.mark_filed(ws, did)
            with mock.patch.object(report_feedback, "read_cloud_auth", side_effect=AssertionError("must not post")):
                self.assertEqual(report_feedback.decide(ws, {}, did, "file"), 0)
            self.assertTrue(report_feedback.filed_receipt(ws, did).exists())
            with mock.patch.object(report_feedback, "hitl_manager", side_effect=OSError("no store")):
                self.assertEqual(report_feedback.pending_clicks(ws), 0, "no store reads as nothing pending")

    def test_an_indeterminate_post_is_held_and_never_re_posted_on_a_guess(self):
        """The in-flight marker covers the window Codex named: a death between the 2xx and the receipt
        (here: mark_filed raising) and a transport error with no answer both leave <id>.posting, and the
        next --apply holds it instead of posting again. A definite server error restores the draft."""
        posted = []
        def _capture(req, timeout=None):
            posted.append(json.loads(req.data.decode())); return _FakeResp()
        from hitl.schema import ActionReply
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"sendLogs": False})
            did = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "once", "body": "b", "auto": True})
            m = self._hitl(ws)
            rid = report_feedback.register_ask(ws, did, "once", "host")
            m.apply_action(ActionReply(hitl_id=rid, expected_revision=1, action_id="file", guard=did))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                # crash after the 2xx, before the receipt
                with mock.patch.object(report_feedback, "mark_filed", side_effect=OSError("died")):
                    with self.assertRaises(OSError):
                        self._run(["--apply"])
                self.assertEqual(len(posted), 1)
                self.assertTrue(report_feedback.posting_marker(ws, did).exists())
                self.assertEqual(report_feedback.list_drafts(ws), [])
                self.assertEqual([d["id"] for d in report_feedback.list_drafts(ws, state="posting")], [did])
                self._run(["--apply"])  # held: no second post; the answered card closes and a new card asks
                self.assertEqual(len(posted), 1, "an indeterminate outcome is never re-posted on a guess")
                self.assertEqual(m.get(rid).status, "resolved", "the click was consumed; the question moved to a new card")
                held = [r for r in m.active() if r.guard == f"{did}:held"]
                self.assertEqual(len(held), 1, "a held draft is owner-visible as its own card")
                self.assertTrue(held[0].turn_on_action)
                self.assertEqual([a.id for a in held[0].actions], ["file", "skip"])
                self._run(["--apply"])  # idempotent: the held card is not duplicated
                self.assertEqual(len([r for r in m.active() if r.guard == f"{did}:held"]), 1)
                # the owner clicks Skip on the held card: the in-flight draft is dropped, the card closes
                m.apply_action(ActionReply(hitl_id=held[0].id, expected_revision=held[0].revision, action_id="skip", guard=held[0].guard))
                self._run(["--apply"])
                self.assertFalse(report_feedback.posting_marker(ws, did).exists())
                self.assertEqual(m.get(held[0].id).status, "resolved")
                self.assertEqual(len(posted), 1)
            # a transport error with no answer is the same shape
            did2 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "two", "body": "b", "auto": True})
            with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback, "post_feedback", side_effect=OSError("connection reset")):
                self.assertEqual(report_feedback.decide(ws, {"sendLogs": False}, did2, "file"), 1)
            self.assertTrue(report_feedback.posting_marker(ws, did2).exists(), "no answer: held, not a draft")
            # a client error proves no write: the draft is a draft again, a retry may post
            did3 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "three", "body": "b", "auto": True})
            bad_req = urllib.error.HTTPError("https://x/api/feedback", 400, "bad", {}, io.BytesIO(b"invalid_payload"))
            with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback, "post_feedback", side_effect=bad_req):
                self.assertEqual(report_feedback.decide(ws, {"sendLogs": False}, did3, "file"), 1)
            self.assertIsNotNone(report_feedback.load_draft(ws, did3), "a 4xx is an answer that proves no write")
            self.assertFalse(report_feedback.posting_marker(ws, did3).exists())
            # a 5xx can follow a committed write: it proves nothing, so it is held like no answer at all
            did4 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "four", "body": "b", "auto": True})
            srv_err = urllib.error.HTTPError("https://x/api/feedback", 500, "boom", {}, io.BytesIO(b"no"))
            with mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback, "post_feedback", side_effect=srv_err):
                self.assertEqual(report_feedback.decide(ws, {"sendLogs": False}, did4, "file"), 1)
            self.assertTrue(report_feedback.posting_marker(ws, did4).exists(), "a 5xx is held, never a free retry")
            self.assertIsNone(report_feedback.load_draft(ws, did4))
            # and an explicit --decide file on an in-flight draft is the owner re-posting on purpose
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=("https://x", "tok")), \
                    mock.patch.object(report_feedback.urllib.request, "urlopen", side_effect=_capture):
                self._run(["--decide", did2, "file"])
            self.assertEqual(len(posted), 2)
            self.assertFalse(report_feedback.posting_marker(ws, did2).exists())

    def test_held_card_edge_cases_settled_by_hand_failing_repost_and_decide_closes_it(self):
        from hitl.schema import ActionReply
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, {"sendLogs": False})
            m = self._hitl(ws)
            # 1. a held card whose draft was settled by hand meanwhile is cancelled, not run
            d1 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "one", "body": "b"})
            h1 = report_feedback.register_held(ws, m, d1, "one", "host")
            report_feedback.drop_draft(ws, d1)
            m.apply_action(ActionReply(hitl_id=h1, expected_revision=1, action_id="file", guard=f"{d1}:held"))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--apply"])
            self.assertEqual(m.get(h1).status, "cancelled")
            # 2. a held click whose re-post fails keeps the card and the in-flight draft
            d2 = report_feedback.write_draft(ws, {"kind": "bug", "severity": "low", "title": "two", "body": "b"})
            report_feedback.mark_posting(ws, d2)
            h2 = report_feedback.register_held(ws, m, d2, "two", "host")
            m.apply_action(ActionReply(hitl_id=h2, expected_revision=1, action_id="file", guard=f"{d2}:held"))
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws), \
                    mock.patch.object(report_feedback, "read_cloud_auth", return_value=(None, None)):
                self._run(["--apply"])
            self.assertEqual(m.get(h2).status, "in_progress")
            # 3. a by-hand --decide on a draft with a held card closes that card too
            with mock.patch.object(report_feedback, "resolve_workspace", return_value=ws):
                self._run(["--decide", d2, "skip"])
            self.assertEqual(m.get(h2).status, "resolved")
            self.assertFalse(report_feedback.posting_marker(ws, d2).exists())

    def test_reply_labels_map_to_decisions(self):
        f = report_feedback.decision_for_reply
        self.assertEqual(f("File this bug report"), "file")
        self.assertEqual(f("File without logs — the log has a client name"), "file_no_logs")
        self.assertEqual(f("skip"), "skip")
        self.assertIsNone(f("thanks"))


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
