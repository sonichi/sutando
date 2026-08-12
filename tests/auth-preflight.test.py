#!/usr/bin/env python3
"""Tests for src/auth_preflight.py — the auth-state probe (sonichi#2396/#2402).

Covers the pure decision (oauthAccount x credentials-file x keychain matrix),
the SSH-context remedy, JSON/exit-code CLI behavior. The keychain check and
env are injected; nothing here touches a real keychain, SSH session, or the
claude binary (the --live path is external I/O, excluded by design).

Run: python3 tests/auth-preflight.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
import auth_preflight  # noqa: E402

check_auth_state = auth_preflight.check_auth_state
main = auth_preflight.main


def _mkconfig(td, oauth=False, creds=False):
    if oauth:
        with open(os.path.join(td, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "x@example.com"}}, f)
    if creds:
        with open(os.path.join(td, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "t"}}, f)
    return td


class TestDecision(unittest.TestCase):
    def test_fresh_empty_dir_requires_login(self):
        with tempfile.TemporaryDirectory() as td:
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "login_required")
            self.assertEqual(len(r["reasons"]), 2)  # no oauthAccount + no creds
            self.assertIn("GUI /login", r["remedy"])
            self.assertIn("restart.sh", r["remedy"])

    def test_remedy_reaches_login_without_looping_through_the_gate(self):
        # Regression (#2413 review blocker): the pre-gate remedy said
        # restart-then-login, but this branch's boot gate makes restart.sh
        # (→ startup.sh → auth-preflight-gate.sh) abort on login_required —
        # so that remedy looped back into the gate and never reached a
        # login-capable CLI. Pin the non-circular shape end-to-end: the
        # FIRST actionable command is a bare CLI launch under the probed
        # config dir (no services, gate never runs), and restart.sh appears
        # only AFTER /login.
        with tempfile.TemporaryDirectory() as td:
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "login_required")
            remedy = r["remedy"]
            self.assertIn(f"CLAUDE_CONFIG_DIR={td} claude", remedy)
            self.assertLess(remedy.index(" claude`"), remedy.index("restart.sh"),
                            "bare-CLI launch must precede the restart step")
            self.assertLess(remedy.index("/login"), remedy.index("restart.sh"),
                            "/login must precede the restart step")
            self.assertNotIn("run `bash src/restart.sh` from the repo, then complete /login",
                             remedy)  # the exact circular phrasing, never again

    def test_remedy_shell_quotes_config_dir_with_spaces(self):
        # Regression (#2413 review P1): the remedy is copy/paste shell
        # syntax — a config dir containing spaces must be shell-quoted or
        # the assignment splits at the first space and the recovery path
        # breaks exactly when the operator needs it.
        import shlex
        with tempfile.TemporaryDirectory() as td:
            spaced = os.path.join(td, "ccd path with spaces")
            os.makedirs(spaced)
            r = auth_preflight.check_auth_state(
                spaced, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "login_required")
            self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(spaced)} claude",
                          r["remedy"])
            self.assertNotIn(f"CLAUDE_CONFIG_DIR={spaced} claude", r["remedy"],
                             "unquoted spaced path must not appear as the command")

    def test_oauth_plus_credentials_file_ok(self):
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True, creds=True)
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "ok")
            self.assertIsNone(r["remedy"])

    def test_oauth_plus_keychain_ok_without_credentials_file(self):
        # The macOS reality from the 2026-07-30 outage: token in Keychain,
        # .credentials.json never exists — must still be OK.
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True)
            r = check_auth_state(td, keychain_check=lambda: True, env={})
            self.assertEqual(r["verdict"], "ok")

    def test_keychain_without_oauth_account_requires_login(self):
        # Keychain item exists but the fresh dir's .claude.json lacks the
        # oauthAccount linkage → CLI treats the install as logged-out.
        with tempfile.TemporaryDirectory() as td:
            r = check_auth_state(td, keychain_check=lambda: True, env={})
            self.assertEqual(r["verdict"], "login_required")
            self.assertTrue(any("oauthAccount" in x for x in r["reasons"]))

    def test_oauth_without_any_credentials_requires_login(self):
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True)
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "login_required")
            self.assertTrue(any("no credentials" in x for x in r["reasons"]))

    def test_empty_oauth_account_counts_as_absent(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": None}, f)
            r = check_auth_state(td, keychain_check=lambda: True, env={})
            self.assertEqual(r["verdict"], "login_required")

    def test_malformed_claude_json_is_login_required_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, ".claude.json"), "w") as f:
                f.write("{not json")
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertEqual(r["verdict"], "login_required")

    def test_ssh_context_prepends_ssh_warning(self):
        with tempfile.TemporaryDirectory() as td:
            r = check_auth_state(td, keychain_check=lambda: False,
                                 env={"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"})
            self.assertTrue(r["ssh"])
            self.assertTrue(r["remedy"].startswith("SSH session detected"))

    def test_no_ssh_no_warning(self):
        with tempfile.TemporaryDirectory() as td:
            r = check_auth_state(td, keychain_check=lambda: False, env={})
            self.assertFalse(r["ssh"])
            self.assertTrue(r["remedy"].startswith("needs GUI /login"))


class TestCli(unittest.TestCase):
    def test_exit_2_and_json_on_fresh_dir(self):
        with tempfile.TemporaryDirectory() as td:
            orig = auth_preflight.keychain_has_credentials
            auth_preflight.keychain_has_credentials = lambda: False
            try:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["--config-dir", td, "--json"])
            finally:
                auth_preflight.keychain_has_credentials = orig
            self.assertEqual(rc, 2)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["verdict"], "login_required")

    def test_exit_0_on_authenticated_dir(self):
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True, creds=True)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--config-dir", td])
            self.assertEqual(rc, 0)
            self.assertIn("OK", buf.getvalue())

    def test_human_output_login_required_prints_reasons_and_remedy(self):
        with tempfile.TemporaryDirectory() as td:
            orig = auth_preflight.keychain_has_credentials
            auth_preflight.keychain_has_credentials = lambda: False
            try:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["--config-dir", td])
            finally:
                auth_preflight.keychain_has_credentials = orig
            out = buf.getvalue()
            self.assertEqual(rc, 2)
            self.assertIn("LOGIN_REQUIRED", out)
            self.assertIn("- no oauthAccount", out)
            self.assertIn("remedy: ", out)

    def test_live_probe_failure_downgrades_static_ok(self):
        # Static PASS but ground truth fails (expired token class): verdict
        # must downgrade, carry the live detail as a reason, and gain a remedy.
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True, creds=True)
            orig = auth_preflight.live_probe
            auth_preflight.live_probe = lambda d, timeout=90: (False, "claude -p exited 1: auth expired")
            try:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["--config-dir", td, "--live", "--json"])
            finally:
                auth_preflight.live_probe = orig
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(out["verdict"], "login_required")
            self.assertFalse(out["live"]["ok"])
            self.assertTrue(any("live probe failed" in r for r in out["reasons"]))
            self.assertIn("GUI /login", out["remedy"])

    def test_live_probe_success_keeps_ok(self):
        with tempfile.TemporaryDirectory() as td:
            _mkconfig(td, oauth=True, creds=True)
            orig = auth_preflight.live_probe
            auth_preflight.live_probe = lambda d, timeout=90: (True, "claude -p ok succeeded")
            try:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["--config-dir", td, "--live", "--json"])
            finally:
                auth_preflight.live_probe = orig
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(out["verdict"], "ok")
            self.assertTrue(out["live"]["ok"])

    def test_exit_3_without_config_dir(self):
        env_had = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        try:
            rc = main(["--config-dir", ""])
        finally:
            if env_had is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = env_had
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
