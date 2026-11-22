#!/usr/bin/env python3
"""claude-local `account`: reading the Anthropic account of an agent's config dir and signing it in
again. Claude Code is a stand-in script here: no real sign-in."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
  if [ -f "$d/stub-wants-code" ]; then
    [ -f "$d/stub-no-link" ] && exit 4
    printf 'Opening browser to sign in\nIf the browser did not open, visit:\n\033[1mhttps://claude.ai/oauth/authorize?code=true&state=s1\033[0m\nPaste code here if prompted > '
    read code || exit 5
    [ "$code" = "good-code#s1" ] || { echo "Couldn't log in: That code wasn't accepted."; exit 1; }
  fi
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


class TwoSteps(Base):
    """A sign-in by someone who is not at the computer: a link out, the page's code back in."""

    def setUp(self):
        super().setUp()
        (self.dir / "stub-wants-code").write_text("")
        self.wait = self.tmp / "account-sign-in"
        self.addCleanup(account.cancel, self.wait)

    def test_start_gives_the_link_and_the_right_code_signs_the_agent_in(self):
        link = account.start(self.cfg, self.env, self.wait)
        self.assertEqual(link, "https://claude.ai/oauth/authorize?code=true&state=s1")
        self.assertTrue(account.waiting(self.wait))
        self.assertEqual(account.give_code(self.wait, "  good-code#s1\n"), (True, ""))
        self.assertEqual(account.status(self.cfg, self.env)["account"], "b\u2026@example.org")
        # Nothing of the sign-in is left behind, the code least of all.
        self.assertFalse(self.wait.exists())
        self.assertEqual(account.give_code(self.wait, "good-code#s1"), (False, account.NOT_WAITING))

    def test_a_wrong_code_is_refused_in_claude_codes_words_and_ends_the_sign_in(self):
        account.start(self.cfg, self.env, self.wait)
        self.assertEqual(account.give_code(self.wait, "wrong-code#s1"), (False, "Couldn't log in: That code wasn't accepted."))
        self.assertFalse(account.waiting(self.wait))
        self.assertEqual(account.words(account.status(self.cfg, self.env)), "I'm not signed in.")
        # What is not a code at all never reaches the waiting sign-in.
        account.start(self.cfg, self.env, self.wait)
        for bad in ("", "short", "two words here", "x" * 3000, "caf\u00e9-code-12345"):
            self.assertIn("does not look like", account.give_code(self.wait, bad)[1], bad)
        self.assertTrue(account.waiting(self.wait))

    def test_a_second_start_replaces_the_first_and_cancel_ends_it(self):
        account.start(self.cfg, self.env, self.wait)
        first = json.loads((self.wait / "pids.json").read_text())
        account.start(self.cfg, self.env, self.wait)
        second = json.loads((self.wait / "pids.json").read_text())
        self.assertNotEqual(first["supervisor"], second["supervisor"])
        for _ in range(50):
            if not account._alive(first["claude"]):
                break
            time.sleep(0.1)
        self.assertFalse(account._alive(first["claude"]), "the first sign-in was ended")
        self.assertEqual((account.cancel(self.wait), account.cancel(self.wait), account.waiting(self.wait)), (True, False, False))

    def test_a_sign_in_that_gives_no_link_or_is_not_allowed_leaves_nothing_waiting(self):
        (self.dir / "stub-no-link").write_text("")
        with self.assertRaises(account.Refused) as refused:
            account.start(self.cfg, self.env, self.wait)
        self.assertIn("gave no link", str(refused.exception))
        self.assertFalse(self.wait.exists())
        for cfg, env, why in (({"CLAUDE": self.cfg["CLAUDE"]}, self.env, account.SHARED),
                              (self.cfg, dict(self.env, ANTHROPIC_BASE_URL="http://localhost:7846"), account.PROXIED)):
            with self.assertRaises(account.Refused) as refused:
                account.start(cfg, env, self.wait)
            self.assertEqual(str(refused.exception), why)
        gone = mock.Mock(side_effect=OSError("no python"))
        with self.assertRaises(account.Refused) as refused:
            account.start(self.cfg, self.env, self.wait, popen=gone)
        self.assertIn("could not be started", str(refused.exception))
        self.assertFalse(self.wait.exists())

    def test_a_sign_in_nobody_finishes_is_ended_and_the_late_code_is_told_so(self):
        self.wait.mkdir()
        os.mkfifo(str(self.wait / "code"))
        (self.wait / "out.log").touch()
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(account.supervise(str(self.wait), self.cfg["CLAUDE"], "claudeai", wait_s=0.5), 0)
        self.assertEqual(json.loads((self.wait / "result.json").read_text()), {"exit": "timeout"})
        self.assertEqual(account.give_code(self.wait, "good-code#s1"), (False, account.NOT_WAITING))

    def test_the_command_runs_both_steps_and_takes_the_code_from_standard_input(self):
        cmd = Command("run_")
        cmd.cfg, cmd.env, cmd.dir = self.cfg, self.env, self.dir
        with mock.patch.object(cli, "config_dir", return_value=self.tmp):
            rc, out = cmd.run_("--start")
            self.assertEqual((rc, out["link"]), (0, "https://claude.ai/oauth/authorize?code=true&state=s1"))
            self.assertIn(out["link"], out["say"])
            with mock.patch.object(sys, "stdin", io.StringIO("good-code#s1\n")):
                rc, out = cmd.run_("--code")
            self.assertEqual((rc, out["changed"], out["account"]), (0, "account", "b\u2026@example.org"))
            with mock.patch.object(sys, "stdin", io.StringIO("good-code#s1\n")):
                rc, out = cmd.run_("--code")
            self.assertEqual((rc, out["detail"]), (1, account.NOT_WAITING))
            cmd.run_("--start")
            self.assertEqual(cmd.run_("--cancel"), (0, {"changed": "", "cancelled": True}))
            rc, out = cmd.run_("--start", env=dict(self.env, ANTHROPIC_BASE_URL="http://localhost:7846"))
            self.assertEqual((rc, out["detail"]), (1, account.PROXIED))


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
