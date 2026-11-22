#!/usr/bin/env python3
"""claude-local `account`: reading the Anthropic account of an agent's config dir and signing it in
again. Claude Code is a stand-in script here: no real sign-in."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ag2_sparrow.adapters.claude_code import account, cli  # noqa: E402

# Claude Code's `auth` as this adapter uses it: status prints what the config dir's `account.json` says
# (else signed out), login records that it ran and with which flags, and signs the dir in as bob.
FAKE_CLAUDE = r'''#!/bin/sh
d="$CLAUDE_CONFIG_DIR"
if [ "$1 $2" = "auth status" ]; then
  [ -f "$d/stub-status-fails" ] && { echo "boom" >&2; exit 3; }
  [ -f "$d/account.json" ] && cat "$d/account.json" || echo '{"loggedIn": false, "authMethod": "none"}'
  exit 0
fi
if [ "$1 $2" = "auth login" ]; then
  echo "login $*" >> "$d/stub-logins"
  [ -f "$d/stub-login-fails" ] && exit 1
  echo '{"loggedIn": true, "authMethod": "claude.ai", "email": "bob@example.org", "subscriptionType": "max"}' > "$d/account.json"
  exit 0
fi
exit 9
'''
ALICE = {"loggedIn": True, "authMethod": "claude.ai", "email": "alice@example.com", "subscriptionType": "pro"}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dir = self.tmp / "claude-config"
        self.dir.mkdir()
        claude = self.tmp / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.cfg = {"CLAUDE": str(claude), "CLAUDE_CONFIG_DIR": str(self.dir)}
        self.env = {"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(self.dir)}

    def signed_in(self, who=ALICE):
        (self.dir / "account.json").write_text(json.dumps(who))


class Status(Base):
    def test_the_account_is_named_with_its_address_masked(self):
        self.signed_in()
        state = account.status(self.cfg, self.env)
        self.assertEqual(state, {"signed_in": True, "method": "claude.ai", "account": "a\u2026@example.com", "plan": "pro",
                                 "proxy": False})
        self.assertEqual(account.words(state), "I'm signed in as a\u2026@example.com (pro).")
        self.assertNotIn("alice", json.dumps(state))

    def test_signed_out_and_an_unreadable_answer_are_told_apart(self):
        self.assertEqual(account.words(account.status(self.cfg, self.env)), "I'm not signed in.")
        self.signed_in({"loggedIn": True, "authMethod": "claude.ai"})
        self.assertEqual(account.words(account.status(self.cfg, self.env)), "I'm signed in as an Anthropic account.")
        # What cannot be read is unknown, never "signed out".
        for bad in ("not json", "[]", '{"loggedIn": "yes"}'):
            (self.dir / "account.json").write_text(bad)
            state = account.status(self.cfg, self.env)
            self.assertEqual((state["signed_in"], account.words(state)), (None, "I could not read which account I'm using."), bad)
        (self.dir / "stub-status-fails").write_text("")
        self.assertIsNone(account.status(self.cfg, self.env)["signed_in"])
        missing = dict(self.cfg, CLAUDE=str(self.tmp / "no-such-claude"))
        self.assertIn("did not answer", account.status(missing, self.env)["detail"])
        self.assertEqual([account.masked(a) for a in ("bob@example.org", "@x.org", "bob@", "bob", "", None)],
                         ["b\u2026@example.org", "", "", "", "", ""])

    def test_an_agent_behind_the_credential_proxy_says_the_proxy_decides_its_account(self):
        self.signed_in()
        state = account.status(self.cfg, dict(self.env, ANTHROPIC_BASE_URL="http://localhost:7846"))
        self.assertEqual((state["proxy"], state["account"]), (True, "a\u2026@example.com"))
        self.assertEqual(account.words(state),
                         "My requests go through the credential proxy on this computer; its sign-in decides my account.")


class SignIn(Base):
    def test_a_sign_in_runs_claude_codes_own_login_in_the_agents_config_dir(self):
        self.signed_in()
        self.assertEqual(account.sign_in(self.cfg, self.env), (True, ""))
        self.assertEqual((self.dir / "stub-logins").read_text().split("\n")[0], "login auth login")
        self.assertEqual(account.status(self.cfg, self.env)["account"], "b\u2026@example.org")
        self.assertEqual(account.sign_in(self.cfg, self.env, console=True), (True, ""))
        self.assertIn("login auth login --console", (self.dir / "stub-logins").read_text())

    def test_a_sign_in_that_fails_times_out_or_cannot_run_says_why(self):
        (self.dir / "stub-login-fails").write_text("")
        self.assertEqual(account.sign_in(self.cfg, self.env), (False, "claude auth login ended with exit code 1"))
        slow = mock.Mock(side_effect=subprocess.TimeoutExpired("claude", 600))
        self.assertEqual(account.sign_in(self.cfg, self.env, run=slow), (False, "the sign-in was not finished in 10 minutes"))
        gone = mock.Mock(side_effect=OSError("no claude"))
        self.assertEqual(account.sign_in(self.cfg, self.env, run=gone), (False, "claude auth login did not run (OSError)"))
        # The sign-in is handed no terminal: it opens the browser itself.
        seen = {}
        account.sign_in(self.cfg, self.env, run=lambda argv, **kw: seen.update(kw, argv=argv) or types.SimpleNamespace(returncode=0))
        self.assertEqual((seen["stdin"], seen["env"], seen["timeout"]), (subprocess.DEVNULL, self.env, account.LOGIN_WAIT_S))

    def test_an_agent_with_no_config_dir_of_its_own_or_behind_the_proxy_is_never_signed_in_here(self):
        for cfg, env, why in (({"CLAUDE": self.cfg["CLAUDE"]}, self.env, account.SHARED),
                              (self.cfg, dict(self.env, ANTHROPIC_BASE_URL="http://localhost:7846"), account.PROXIED)):
            with self.assertRaises(account.Refused) as refused:
                account.sign_in(cfg, env)
            self.assertEqual(str(refused.exception), why)
        self.assertFalse((self.dir / "stub-logins").exists())


class Command(Base):
    """`ag2-sparrow-claude-local account`, with the install's config stood in."""

    def run_(self, *argv, cfg=None, problem="", env=None):
        out = io.StringIO()
        with mock.patch.object(cli, "selected", return_value=cli.LOCAL), \
                mock.patch.object(cli, "load_config", return_value=(self.cfg if cfg is None else cfg, problem)), \
                mock.patch.object(cli, "account_env", return_value=self.env if env is None else env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["account", *argv])
        return rc, (json.loads(out.getvalue()) if out.getvalue() else None)

    def test_account_prints_the_account_and_changes_nothing(self):
        self.signed_in()
        rc, out = self.run_()
        self.assertEqual((rc, out), (0, {"changed": "", "signed_in": True, "method": "claude.ai", "account": "a\u2026@example.com",
                                         "plan": "pro", "proxy": False, "say": "I'm signed in as a\u2026@example.com (pro)."}))
        self.assertFalse((self.dir / "stub-logins").exists())

    def test_switch_signs_in_and_says_the_session_must_restart(self):
        self.signed_in()
        rc, out = self.run_("--switch")
        self.assertEqual((rc, out["changed"], out["account"]), (0, "account", "b\u2026@example.org"))
        self.assertIn("when it next starts", out["restart"])
        self.run_("--switch", "--console")
        self.assertIn("--console", (self.dir / "stub-logins").read_text())
        (self.dir / "stub-login-fails").write_text("")
        rc, out = self.run_("--switch")
        self.assertEqual((rc, out["changed"], out["detail"]), (1, "", "claude auth login ended with exit code 1"))

    def test_refusals_and_a_missing_install_change_nothing(self):
        rc, out = self.run_("--switch", cfg={"CLAUDE": self.cfg["CLAUDE"]})
        self.assertEqual((rc, out["changed"], out["detail"]), (1, "", account.SHARED))
        rc, out = self.run_("--switch", env=dict(self.env, ANTHROPIC_BASE_URL="http://localhost:7846"))
        self.assertEqual((rc, out["changed"], out["detail"], out["proxy"]), (1, "", account.PROXIED, True))
        self.assertFalse((self.dir / "stub-logins").exists())
        self.assertEqual(self.run_(problem="no install"), (cli.EXIT_UNUSABLE, None))
        with mock.patch.object(cli, "selected", return_value=None):
            self.assertEqual(cli.main(["account"]), cli.EXIT_UNUSABLE)

    def test_the_proxy_address_is_read_from_the_agents_running_session(self):
        """The command's caller (the app, a terminal) may not have the variable the session was started with."""
        answers = iter([types.SimpleNamespace(returncode=0, stdout="ANTHROPIC_BASE_URL=http://localhost:7846\n"),
                        types.SimpleNamespace(returncode=0, stdout="-ANTHROPIC_BASE_URL\n"),
                        types.SimpleNamespace(returncode=1, stdout=""), None])
        with mock.patch.object(cli, "child_env", side_effect=lambda cfg: dict(self.env)), \
                mock.patch.object(cli.launcher, "configured_tmux", return_value=("/tmux", "")), \
                mock.patch.object(cli.launcher.Tmux, "run", side_effect=lambda *a, **k: next(answers)) as asked:
            got = [cli.account_env(self.cfg, cli.LOCAL).get("ANTHROPIC_BASE_URL") for _ in range(4)]
        self.assertEqual(got, ["http://localhost:7846", None, None, None])
        self.assertEqual(asked.call_args.args, ("show-environment", "-g", "ANTHROPIC_BASE_URL"))
        with mock.patch.object(cli, "child_env", return_value={"ANTHROPIC_BASE_URL": "http://own"}), \
                mock.patch.object(cli.launcher, "configured_tmux", return_value=("", "no tmux")):
            self.assertEqual(cli.account_env(self.cfg, cli.LOCAL), {"ANTHROPIC_BASE_URL": "http://own"})


if __name__ == "__main__":
    unittest.main()
