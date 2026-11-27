"""Codex's device-code sign-in from the owner's phone (`adapters/codex/device_login`), against a stand-in
`codex` that prints what codex 0.160.0 prints and then signs in, fails or never finishes.

No real `codex` runs and no real login is touched: the user's home as the module sees it, the agent's
CODEX_HOME and the stand-in live in a temp dir, and the stand-in's "login" is a JSON file with a made-up id
token. HOME itself is left alone: the stub app-server (run under coverage in CI) needs the real one. Run:
    python3 packages/ag2-sparrow/tests/test_codex_device_login.py
"""
import errno
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from ag2_sparrow import account_switch, connector_cards, detached_login, signin_watch  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc, device_login as dl  # noqa: E402

from test_codex_connector import CardWorld, metrics, result, run_connector  # noqa: E402

LINK, CODE = "https://auth.openai.com/codex/device", "ABCD-12345"
# What codex 0.160.0 prints for `login --device-auth`, colours and all.
PRINTED = ("\x1b[1mWelcome to Codex\x1b[0m [v0.160.0]\n\x1b[2mOpenAI's command-line coding agent\x1b[0m\n\n"
           "Follow these steps to sign in with ChatGPT using device code authorization:\n\n"
           "1. Open this link in your browser and sign in\n   \x1b[94m%s\x1b[0m\n\n"
           "2. Enter this one-time code after you are signed in \x1b[2m(expires in 15 minutes)\x1b[0m\n"
           "   \x1b[94m%s\x1b[0m\n\n\x1b[2mDevice codes are a common phishing target. Never share this code.\x1b[0m\n"
           % (LINK, CODE))

# The stand-in `codex`: `mode` says how a sign-in ends ("ok <email>", "wait <email>" once `go` exists, "fail",
# "silent", "stubborn" with a grandchild, both deaf to SIGTERM, else never); `calls` logs each call and its CODEX_HOME.
STUB = r'''
import base64, json, os, signal, subprocess, sys, time
here = sys.argv[1]
args = sys.argv[2:]
if args == ["grandchild"]:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(600)
home = os.environ.get("CODEX_HOME", "")
with open(os.path.join(here, "calls"), "a") as f:
    f.write(json.dumps([home] + args) + "\n")
try:
    mode = open(os.path.join(here, "mode")).read().strip()
except OSError:
    mode = ""
if args == ["logout"]:
    if mode == "logout-fail":
        sys.exit(1)
    os.unlink(os.path.join(home, "auth.json"))
    print("Successfully logged out")
    sys.exit(0)
if args != ["login", "--device-auth"]:
    sys.exit(2)
with open(os.path.join(here, "pid"), "w") as f:
    f.write(str(os.getpid()))
if mode != "silent":
    sys.stdout.write(PRINTED)
    sys.stdout.flush()
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    kid = subprocess.Popen([sys.executable, __file__, here, "grandchild"])
    with open(os.path.join(here, "grandpid"), "w") as f:
        f.write(str(kid.pid))
    time.sleep(600)
quick = mode.startswith("wait ")
if quick:
    while not os.path.exists(os.path.join(here, "go")):
        time.sleep(0.05)
    mode = "ok " + mode[5:]
if mode.startswith("ok "):
    time.sleep(0 if quick else 1.0)
    claims = base64.urlsafe_b64encode(json.dumps({"email": mode[3:]}).encode()).decode().rstrip("=")
    with open(os.path.join(home, "auth.json"), "w") as f:
        json.dump({"tokens": {"account_id": "acct-1", "id_token": "e30." + claims + ".sig"}}, f)
    print("Successfully logged in")
    sys.exit(0)
if mode == "fail":
    print("Error logging in with device code: device code login is not enabled for this workspace")
    sys.exit(1)
time.sleep(600)
'''.replace("PRINTED", repr(PRINTED), 1)


def shq(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


class Home:
    """The user's home (as device_login sees it), an agent's live CODEX_HOME and the stand-in `codex`, all in a
    temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.user, self.home, self.bin, self.state = root / "user", root / "agent-codex-home", root / "bin", root / "state"
        for d in (self.user, self.home, self.bin, self.state):
            d.mkdir()
        (self.bin / "stub.py").write_text(STUB)
        self.codex = self.bin / "codex"
        # A shell wrapper: the interpreter's path may hold a space, which no #! line can carry.
        self.codex.write_text("#!/bin/sh\nexec %s %s %s \"$@\"\n" % (shq(sys.executable), shq(self.bin / "stub.py"),
                                                                    shq(self.bin)))
        self.codex.chmod(0o755)
        self.env = {"PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "/usr/bin:/bin")), "HOME": os.environ.get("HOME", ""),
                    "CODEX_HOME": str(self.home)}
        self.dir = self.state / dl.STATE
        for patch in (mock.patch.dict(os.environ, self.env), mock.patch.object(dl, "_user_home", return_value=self.user)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: dl.cancel(self.dir, str(self.codex), self.env))     # first: cleanups run last-in first-out

    def mode(self, how):
        (self.bin / "mode").write_text(how)

    def calls(self):
        p = self.bin / "calls"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def staging(self):
        return os.path.abspath(self.dir / dl.STAGING)

    def finished(self, timeout=15):
        end = time.time() + timeout
        # Over, or already told: a connector's loop may have looked after it before this looks.
        while self.dir.exists() and not (self.dir / "result.json").exists():
            if time.time() > end:
                self.fail("the sign-in never ended")
            time.sleep(0.05)

    def stub_gone(self, timeout=10, name="pid"):
        pid = int((self.bin / name).read_text())
        end = time.time() + timeout
        while time.time() < end:
            state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout
            if not state.strip() or "Z" in state:
                return True
            time.sleep(0.05)
        return False

    def live_email(self):
        found = signin_watch.codex_identity(str(self.home))
        return found[1] if found else None


class ParseTest(unittest.TestCase):
    def test_the_link_and_code_are_read_from_what_codex_prints(self):
        self.assertEqual(dl.parse(PRINTED), (LINK, CODE))

    def test_a_code_on_the_line_that_asks_for_it_and_partial_output(self):
        self.assertEqual(dl.parse("Open %s.\nEnter this one-time code: WXYZ-9876\n" % LINK), (LINK, "WXYZ-9876"))
        self.assertEqual(dl.parse("1. Open this link\n   %s\n" % LINK), (LINK, ""), "the code is not printed yet")
        self.assertEqual(dl.parse("2. Enter this one-time code (expires in 15 minutes)\n\n"), ("", ""))
        self.assertEqual(dl.parse("Enter this one-time code (expires in 15 minutes)\n   not a code!\n"), ("", ""))

    def test_a_link_that_is_not_openais_is_never_shown(self):
        for url in ("https://auth.openai.com.evil.test/codex/device", "http://auth.openai.com/codex/device",
                    "https://user@auth.openai.com/x", "https://example.com/codex/device"):
            with self.subTest(url=url):
                self.assertFalse(dl.link_ok(url))
                self.assertEqual(dl.parse("Open %s\nEnter this one-time code\n%s\n" % (url, CODE))[0], "")
        self.assertTrue(dl.link_ok("https://chatgpt.com/device"))

    def test_the_users_home_is_the_one_the_process_runs_as(self):
        self.assertEqual(dl._user_home(), Path(os.path.expanduser("~")))


class OwnHomeTest(Home, unittest.TestCase):
    def test_only_the_agents_own_home_is_signed_in(self):
        self.assertEqual(dl.own_home(self.env, [str(self.home)]), str(self.home))
        with self.assertRaises(dl.Refused) as e:
            dl.own_home(dict(self.env, CODEX_HOME=""))
        self.assertEqual(str(e.exception), dl.SHARED)
        (self.user / ".codex").mkdir()
        with self.assertRaises(dl.Refused) as e:
            dl.own_home(dict(self.env, CODEX_HOME=str(self.user / ".codex")))
        self.assertEqual(str(e.exception), dl.PERSONAL)
        with self.assertRaises(dl.Refused) as e:
            dl.own_home(self.env, [str(self.home), str(self.home) + "/"])
        self.assertEqual(str(e.exception), dl.ANOTHER_AGENTS)
        core = Path(self.tmp.name) / "core"
        core.mkdir()
        (core / "auth.json").write_text("{}")
        os.symlink(core / "auth.json", self.home / "auth.json")
        with self.assertRaises(dl.Refused) as e:
            dl.own_home(self.env)
        self.assertEqual(str(e.exception), dl.CORES)

    def test_the_homes_of_the_installed_jobs_are_read_from_their_configs(self):
        self.assertEqual(dl.installed_homes(), [])
        agents = self.user / ".config" / "ag2-sparrow" / "agents"
        for key, home in (("a", self.home), ("b", "")):
            (agents / key).mkdir(parents=True)
            (agents / key / "codex-local.conf").write_text("CODEX=codex\nCODEX_HOME=%s\n" % home)
        self.assertEqual(dl.installed_homes(), [str(self.home)])
        (agents / "c").mkdir()
        (agents / "c" / "codex-local.conf").write_text('CODEX_HOME="unterminated\n')
        (agents / "d.conf").mkdir()
        self.assertEqual(dl.installed_homes(), [str(self.home)], "an unreadable or unparsable config names none")
        with mock.patch.object(dl.Path, "rglob", side_effect=OSError):
            self.assertEqual(dl.installed_homes(), [])


class SignInTest(Home, unittest.TestCase):
    def start(self, **kw):
        return dl.start(str(self.codex), self.env, self.dir, **kw)

    def over(self, **kw):
        return dl.over(self.dir, str(self.codex), self.env, **kw)

    def test_the_sign_in_goes_to_a_staging_home_and_is_used_only_on_yes(self):
        (self.home / "auth.json").write_text('{"tokens": {"account_id": "old"}}')
        self.mode("ok bob@example.org")
        meta, again = self.start()
        self.assertEqual((meta["phase"], meta["link"], meta["code"], again), ("code", LINK, CODE, False))
        self.assertEqual(oct(self.dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.dir / dl.STAGING).stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.dir / "meta.json").stat().st_mode & 0o777), "0o600")
        if dl.current(self.dir).get("phase") == "code":
            self.assertEqual(self.start()[1], True, "a second start shows the same")
        self.assertEqual(self.calls(), [[self.staging(), "login", "--device-auth"]], "one sign-in, on the staging home")
        dl.note_card(self.dir, "!dm:s.space", "$card1")
        self.finished()
        self.assertEqual(self.over(), {"phase": "confirm", "email": "bob@example.org", "room": "!dm:s.space", "cards": ["$card1"],
                                       "edit": "$card1"})
        meta = json.loads((self.dir / "meta.json").read_text())
        self.assertNotIn("code", meta, "the code is dropped once Codex is done")
        self.assertFalse((self.dir / "out.log").exists())
        self.assertEqual(self.live_email(), "", "the live login is untouched until Yes")
        self.assertEqual(self.over(), {}, "waiting for the answer")
        self.assertEqual(self.start()[0]["phase"], "confirm", "a tap meanwhile shows the question again")
        self.assertEqual(dl.answer(self.dir, "$other", True, str(self.codex), self.env)[0], "stale",
                         "only the card that asked")
        self.assertEqual(dl.answer(self.dir, "$card1", True, str(self.codex), self.env), ("used", "bob@example.org", ["$card1"]))
        self.assertEqual(self.live_email(), "bob@example.org")
        self.assertEqual(oct((self.home / "auth.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["auth.json"], "no earlier login left beside it")
        self.assertFalse(self.dir.exists())
        self.assertEqual(dl.answer(self.dir, "$card1", True, str(self.codex), self.env)[0], "stale", "first answer only")

    def test_no_signs_the_staged_account_out_and_deletes_it(self):
        self.mode("ok bob@example.org")
        self.start()
        dl.note_card(self.dir, "!dm:s.space", "$card1")
        self.finished()
        self.over()
        staging = self.staging()
        self.assertEqual(dl.answer(self.dir, "$card1", False, str(self.codex), self.env), ("discarded", "bob@example.org", ["$card1"]))
        self.assertEqual(self.calls()[-1], [staging, "logout"])
        self.assertFalse(self.dir.exists())
        self.assertFalse((self.home / "auth.json").exists())

    def test_no_answer_in_time_signs_the_staged_account_out(self):
        self.mode("ok bob@example.org")
        self.start()
        self.finished()
        self.over()
        staging = self.staging()
        self.assertEqual(self.over(now=time.time() + dl.LOGIN_WAIT_S + 1)["exit"], "unanswered")
        self.assertEqual(self.calls()[-1], [staging, "logout"])
        self.assertFalse(self.dir.exists())

    def test_a_late_answer_is_refused_where_it_is_applied_even_with_no_check_in_between(self):
        self.mode("ok bob@example.org")
        self.start()
        dl.note_card(self.dir, "!dm:s.space", "$card1")
        self.finished()
        self.over()
        staging = self.staging()
        late = time.time() + dl.LOGIN_WAIT_S + 1
        self.assertEqual(dl.answer(self.dir, "$card1", True, str(self.codex), self.env, now=late),
                         ("expired", "bob@example.org", ["$card1"]))
        self.assertFalse((self.home / "auth.json").exists(), "the live login is untouched")
        self.assertEqual(self.calls()[-1], [staging, "logout"])
        self.assertFalse(self.dir.exists())

    def test_a_sign_in_deaf_to_sigterm_and_its_child_are_killed_by_exact_pid(self):
        self.mode("stubborn")
        with mock.patch.object(dl, "STOP_WAIT_S", 0.3):
            self.start()
            end = time.time() + 10
            while not (self.bin / "grandpid").exists() and time.time() < end:
                time.sleep(0.05)
            self.assertTrue(dl.cancel(self.dir, str(self.codex), self.env))
        self.assertTrue(self.stub_gone(timeout=3), "the sign-in")
        self.assertTrue(self.stub_gone(timeout=3, name="grandpid"), "its descendant")

    def test_supervise_kills_a_sign_in_deaf_to_sigterm_at_its_time(self):
        self.mode("stubborn")
        self.dir.mkdir()
        with mock.patch.object(dl, "STOP_WAIT_S", 0.3):
            dl.supervise(str(self.dir), str(self.codex), wait_s=1.0)
        self.assertEqual(detached_login.read_json(self.dir / "result.json"), {"exit": "timeout"})
        self.assertTrue(self.stub_gone(timeout=3))
        self.assertTrue(self.stub_gone(timeout=3, name="grandpid"))

    def test_a_staged_login_whose_account_cannot_be_read_is_not_offered(self):
        self.mode("ok -")
        self.start()
        self.finished()
        done = self.over()
        self.assertEqual(done["exit"], 0)
        self.assertIn("could not read which account", done["why"])
        self.assertFalse(self.dir.exists())

    def test_a_yes_that_cannot_be_carried_out_keeps_the_live_login(self):
        (self.home / "auth.json").write_text("old")
        self.mode("ok bob@example.org")
        self.start()
        dl.note_card(self.dir, "!dm:s.space", "$card1")
        self.finished()
        self.over()
        real = os.replace
        with mock.patch.object(dl.os, "replace", side_effect=lambda a, b: real(a, b) if b != str(self.home / "auth.json")
                               else (_ for _ in ()).throw(OSError(errno.EACCES, "no"))):
            outcome, why, _ = dl.answer(self.dir, "$card1", True, str(self.codex), self.env)
        self.assertEqual((outcome, why), ("not-used", "the login could not be moved (PermissionError)"))
        self.assertEqual((self.home / "auth.json").read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["auth.json"])
        self.assertFalse(self.dir.exists())

    def test_a_staged_login_on_another_file_system_is_copied_then_renamed_into_place(self):
        staged = self.state / "auth.json"
        staged.write_text("new")
        real, real_copy, modes = os.replace, dl.shutil.copyfileobj, []

        def copy(src, dst):     # the copy's mode while its bytes go in: private from the start
            modes.append(oct(os.fstat(dst.fileno()).st_mode & 0o777))
            real_copy(src, dst)
        old = os.umask(0o022)
        try:
            with mock.patch.object(dl.os, "replace", side_effect=lambda a, b: (_ for _ in ()).throw(
                    OSError(errno.EXDEV, "cross")) if a == str(staged) else real(a, b)), \
                    mock.patch.object(dl.shutil, "copyfileobj", side_effect=copy):
                dl.swap(staged, self.home)
        finally:
            os.umask(old)
        self.assertEqual(modes, ["0o600"])
        self.assertEqual((self.home / "auth.json").read_text(), "new")
        self.assertEqual(oct((self.home / "auth.json").stat().st_mode & 0o777), "0o600")
        with self.assertRaises(OSError):
            dl.swap(self.state / "missing.json", self.home)
        with mock.patch.object(dl.os, "replace", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                dl.swap(staged, self.home)

    def test_a_sign_in_codex_refuses_says_why_without_the_code(self):
        self.mode("fail")
        self.start()
        self.finished()
        done = self.over()
        self.assertEqual(done["exit"], 1)
        self.assertIn("device code login is not enabled", done["why"])
        self.assertFalse(self.dir.exists())
        self.assertEqual(dl._why("x\n%s\n%s\n" % (CODE, LINK), CODE, LINK), "x")

    def test_a_sign_in_nobody_finishes_is_ended_after_its_time(self):
        self.mode("hang")
        self.start(login_wait_s=0.5)
        self.finished()
        self.assertEqual(self.over()["exit"], "timeout")
        self.assertTrue(self.stub_gone(), "codex's own polling was stopped")

    def test_a_cancel_ends_the_running_sign_in(self):
        self.mode("hang")
        self.start()
        self.assertTrue(dl.current(self.dir))
        self.assertTrue(dl.cancel(self.dir, str(self.codex), self.env))
        self.assertTrue(self.stub_gone())
        self.assertFalse(dl.cancel(self.dir, str(self.codex), self.env))

    def test_no_link_and_code_in_time_starts_nothing(self):
        self.mode("silent")
        with self.assertRaises(dl.Refused):
            self.start(wait_s=1.0)
        self.assertFalse(self.dir.exists())
        self.assertTrue(self.stub_gone())

    def test_a_sign_in_that_cannot_be_started_or_refuses_the_home(self):
        with self.assertRaises(dl.Refused) as e:
            self.start(popen=mock.Mock(side_effect=OSError))
        self.assertIn("could not be started (OSError)", str(e.exception))
        with self.assertRaises(dl.Refused):
            dl.start(str(self.codex), dict(self.env, CODEX_HOME=""), self.dir)
        self.assertEqual(self.calls(), [])

    def test_a_supervisor_that_is_gone_is_taken_as_lost_and_late_as_timed_out(self):
        self.dir.mkdir()
        dl.write_private(self.dir / "pids.json", {"supervisor": [999999, "never"]})
        dl.write_private(self.dir / "meta.json", {"phase": "code", "link": LINK, "code": CODE, "at": time.time()})
        self.assertEqual(self.over()["exit"], "lost")
        self.dir.mkdir()
        dl.write_private(self.dir / "meta.json", {"phase": "code", "at": time.time() - 1000})
        self.assertEqual(self.over()["exit"], "timeout")

    def test_a_staged_logout_that_cannot_run_still_deletes_the_staged_login(self):
        (self.dir / dl.STAGING).mkdir(parents=True)
        (self.dir / dl.STAGING / "auth.json").write_text("{}")
        self.assertTrue(dl.cancel(self.dir, str(self.codex), self.env, run=mock.Mock(side_effect=OSError)))
        self.assertFalse(self.dir.exists())

    def test_supervise_records_how_codex_ended(self):
        self.mode("hang")
        self.dir.mkdir()
        self.assertEqual(dl.supervise(str(self.dir), str(self.codex), wait_s=0.5), 0)
        self.assertEqual(detached_login.read_json(self.dir / "result.json"), {"exit": "timeout"})
        self.assertEqual(dl.parse((self.dir / "out.log").read_text()), (LINK, CODE))
        self.mode("fail")
        self.assertEqual(dl.supervise(str(self.dir), str(self.codex), wait_s=10), 0)
        self.assertEqual(detached_login.read_json(self.dir / "result.json"), {"exit": 1})

    def test_sign_out_runs_codex_logout_on_the_agents_home_only(self):
        (self.home / "auth.json").write_text("{}")
        self.assertEqual(dl.sign_out(str(self.codex), self.env), (True, ""))
        self.assertFalse((self.home / "auth.json").exists())
        self.mode("logout-fail")
        self.assertEqual(dl.sign_out(str(self.codex), self.env), (False, "codex logout ended with exit code 1"))
        self.assertEqual(dl.sign_out(str(self.codex), self.env, run=mock.Mock(side_effect=OSError)),
                         (False, "codex logout did not run (OSError)"))
        with self.assertRaises(dl.Refused):
            dl.sign_out(str(self.codex), dict(self.env, CODEX_HOME=str(self.user / ".codex")))
        self.assertEqual(self.calls(), [[str(self.home), "logout"]] * 2)


class RemoteCardTest(Home, CardWorld, unittest.TestCase):
    """The account card's "Add account", Yes / No and "Sign out" in the connector, end to end on the stub server."""

    def setUp(self):
        Home.setUp(self)
        CardWorld.setUp(self)
        self.dir = self.work / "state" / dl.STATE
        (self.home / "stub-account").write_text("alice@example.com")

    def tearDown(self):
        dl.cancel(self.dir, str(self.codex), self.env)      # its processes, before the work root goes
        CardWorld.tearDown(self)

    def run_codex(self, once=1):
        self.assertEqual(run_connector(self.work, once=once, extra=["--codex", str(self.codex)]), 0)

    def ask(self, event):
        self.dm("task-ask-" + event[1:], "/account")
        self.run_codex()
        ((_, card),) = self.posted(event)
        return card

    def tap(self, tid, card, label, event, **kw):
        said = next(b["reply"] for b in card["buttons"] if b["label"] == label)
        self.CARD = card["id"]
        self.dm(tid, said, reply_to=event, click=said, **kw)
        self.run_codex()
        return result(self.work, tid)

    def hello(self, tid):
        """An ordinary message: the connector's loop looks after the sign-in while it is served."""
        self.dm(tid, "hello")
        self.run_codex()
        self.assertTrue(result(self.work, tid).startswith("STUB echo:"))

    def confirm(self):
        """Add an account up to the question; the question card."""
        self.mode("ok bob@example.org")
        card = self.ask("$card1")
        self.assertEqual([(b["label"], b.get("command")) for b in card["buttons"]],
                         [("Switch account", "card:account:agent:switch"), ("Add account", "card:account:agent:add"),
                          (account_switch.HERE_LABEL, "card:account:agent:here")])
        self.assertEqual(self.tap("task-add", card, "Add account", "$card1"), account_switch.ANSWERED)
        outbox = self.bridge.joinpath(*connector_cards.OUTBOX)
        self.assertEqual({oct(p.stat().st_mode & 0o777) for p in outbox.iterdir()} | {oct(outbox.stat().st_mode & 0o777)},
                         {"0o600", "0o700"}, "the card carrying the code is readable by its owner only")
        ((room, shown),) = self.posted("$card1")
        posted = self.bridge.joinpath(*connector_cards.POSTED)
        self.assertEqual({oct(p.stat().st_mode & 0o777) for p in posted.iterdir()}, {"0o600"})
        self.assertEqual(self.ops(), [("edit", "$card1")], "shown in place of the card tapped")
        self.assertEqual((room, shown["id"], shown["state"]), (self.DM, dl.DEVICE_CARD, "pending"))
        self.assertIn("open the sign-in page at auth.openai.com", shown["text"])
        self.assertIn("enter this one-time code: %s. It works for 5 minutes." % CODE, shown["text"])
        self.assertEqual([(b["kind"], b["label"], b["url"]) for b in shown["buttons"]],
                         [("url", "Open auth.openai.com", LINK)])
        self.assertAlmostEqual(shown["expires_at"] / 1000, time.time() + dl.LOGIN_WAIT_S, delta=30)
        self.assertEqual(metrics(self.work)[-1]["card"], "account-device-started")
        self.assertNotIn(CODE, result(self.work, "task-add"))
        self.finished()
        self.hello("task-next")
        ((_, asked),) = self.posted("$card1")
        self.assertEqual(self.ops(), [("edit", "$card1")], "the link and code are replaced in place")
        self.assertEqual(asked["text"], "Signed in as bob@example.org. Use this account for Codex? I'm using "
                                        "alice@example.com.")
        self.assertNotIn(CODE, json.dumps(asked))
        self.assertNotIn("auth.openai.com", json.dumps(asked))
        self.assertEqual([(b["label"], b["command"]) for b in asked["buttons"]],
                         [("Yes", "card:account:agent:use"), ("No", "card:account:agent:discard")])
        self.assertFalse((self.home / "auth.json").exists(), "the live login is untouched until Yes")
        return asked

    def test_add_account_then_yes_uses_it_and_sign_out_works(self):
        asked = self.confirm()
        self.assertEqual(self.tap("task-yes", asked, "Yes", "$card1"), account_switch.ANSWERED)
        ((_, done),) = self.posted("$card1")
        self.assertEqual((done["state"], done["text"]), ("done", "Now using bob@example.org for Codex."))
        self.assertEqual([(b["label"], b["command"], b["disabled"]) for b in done["buttons"]],
                         [("Sign out", "card:account:agent:signout", False)])
        self.assertEqual(self.live_email(), "bob@example.org")
        self.assertEqual(metrics(self.work)[-1]["card"], "account-device-used")
        self.assertEqual(self.tap("task-out", done, "Sign out", "$card1"), account_switch.ANSWERED)
        ((_, out),) = self.posted("$card1")
        self.assertEqual(out["text"], "Signed out. I'm not signed in. " + account_switch.BELOW)
        self.assertFalse((self.home / "auth.json").exists())
        self.assertEqual(self.calls()[-1], [str(self.home), "logout"])

    def test_no_keeps_the_account_in_use_and_a_later_answer_does_nothing(self):
        asked = self.confirm()
        self.assertEqual(self.tap("task-no", asked, "No", "$card1"), account_switch.ANSWERED)
        ((_, kept),) = self.posted("$card1")
        self.assertEqual(kept["text"], "Not added: I signed bob@example.org out again. I'm using alice@example.com. "
                         + account_switch.BELOW)
        self.assertEqual(metrics(self.work)[-1]["card"], "account-device-discarded")
        self.assertEqual(self.calls()[-1][1:], ["logout"])
        self.assertNotEqual(self.calls()[-1][0], str(self.home), "the staged login is signed out, never the live one")
        # A second answer on a card that is no longer asking changes nothing.
        self.assertEqual(dl.answer(self.dir, "$card1", True, str(self.codex), self.env), ("stale", "", []))
        self.assertFalse((self.home / "auth.json").exists())

    def test_a_second_tap_while_one_runs_shows_the_same_code_and_starts_nothing(self):
        self.mode("hang")
        self.tap("task-add-1", self.ask("$card1"), "Add account", "$card1")
        self.posted("$card1")
        self.assertEqual(self.tap("task-add-2", self.ask("$card2"), "Add account", "$card2"), account_switch.ANSWERED)
        ((_, again),) = self.posted("$card2")
        self.assertIn(CODE, again["text"])
        self.assertEqual(metrics(self.work)[-1]["card"], "account-device-again")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(detached_login.read_json(self.dir / "meta.json")["edit"], "$card2", "told on the newest card")

    def test_every_card_that_showed_the_code_is_withdrawn_when_the_sign_in_ends(self):
        self.mode("wait bob@example.org")
        self.tap("task-add-1", self.ask("$card1"), "Add account", "$card1")
        self.posted("$card1")
        self.tap("task-add-2", self.ask("$card2"), "Add account", "$card2")
        ((_, again),) = self.posted("$card2")
        self.assertIn(CODE, again["text"])
        (self.bin / "go").write_text("")
        self.finished()
        self.hello("task-next")
        cards = self.posted("$card3")
        shown = dict(zip([e for _, e in self.ops()], [c for _, c in cards]))
        self.assertEqual(sorted(shown), ["$card1", "$card2"])
        self.assertTrue(shown["$card2"]["text"].startswith("Signed in as bob@example.org."), "the question, on the newest")
        self.assertEqual((shown["$card1"]["id"], shown["$card1"]["state"]), (dl.WITHDRAWN_CARD, "expired"))
        self.assertNotIn(CODE, json.dumps(shown))

    def test_a_sign_in_not_finished_in_time_is_stopped_and_its_card_withdrawn(self):
        self.mode("hang")
        with mock.patch.object(dl, "LOGIN_WAIT_S", 0.5):
            self.tap("task-add", self.ask("$card1"), "Add account", "$card1")
            self.posted("$card1")
            self.finished()
            self.hello("task-next")
        ((_, expired),) = self.posted("$card1")
        self.assertEqual(self.ops(), [("edit", "$card1")])
        self.assertEqual((expired["id"], expired["state"]), (account_switch.CARD, "error"))
        self.assertTrue(expired["text"].startswith("The sign-in expired before it was finished, so I stopped it. Tap "
                                                   "Add account to start again."))
        self.assertNotIn(CODE, json.dumps(expired))
        self.assertTrue(self.stub_gone())

    def test_a_question_nobody_answers_is_withdrawn_and_the_staged_account_signed_out(self):
        self.confirm()
        with mock.patch.object(dl, "LOGIN_WAIT_S", -1):
            self.hello("task-later")
        ((_, gone),) = self.posted("$card1")
        self.assertTrue(gone["text"].startswith("You didn't answer within"))
        self.assertEqual(self.calls()[-1][1:], ["logout"])
        self.assertFalse(self.dir.exists())

    def test_a_sign_in_codex_ends_with_an_error_says_why(self):
        self.mode("fail")
        self.tap("task-add", self.ask("$card1"), "Add account", "$card1")
        self.finished()
        self.hello("task-next")
        *_, (_, failed) = self.posted("$card1")
        self.assertEqual(failed["state"], "error")
        self.assertIn("The sign-in did not finish (Error logging in with device code: device code login is not "
                      "enabled for this workspace). I'm using alice@example.com.", failed["text"])

    def test_only_the_owners_tap_in_their_dm_on_a_recent_card_starts_a_sign_in(self):
        self.mode("hang")
        card = self.ask("$card1")
        self.assertIn("couldn't match that button", self.tap("task-guest", card, "Add account", "$card1", tier="guest"))
        self.assertIn("couldn't match that button", self.tap("task-room", card, "Add account", "$card1", kind="room"))
        self.assertIn("couldn't match that button", self.tap("task-other", card, "Add account", "$card1",
                                                             channel="!room:s.space", kind="room"))
        with mock.patch.object(dl, "TAP_FRESH_S", -1):
            self.assertEqual(self.tap("task-old", card, "Add account", "$card1"), cc.quoted(dl.STALE_CARD) + "\n")
        self.assertEqual([m["card"] for m in metrics(self.work)[-4:]],
                         ["card-refused", "card-refused", "card-refused", "account-card-stale"])
        self.assertEqual((self.calls(), self.dir.exists()), ([], False))

    def test_a_home_that_is_not_the_agents_own_is_refused_on_the_card(self):
        (self.user / ".codex").mkdir()
        (self.user / ".codex" / "stub-account").write_text("alice@example.com")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.user / ".codex")}):
            self.tap("task-add", self.ask("$card1"), "Add account", "$card1")
        ((_, refused),) = self.posted("$card1")
        self.assertEqual(refused["state"], "error")
        self.assertIn("I could not start a sign-in: " + dl.PERSONAL, refused["text"])
        self.assertEqual(metrics(self.work)[-1]["card"], "account-not-started")
        self.assertEqual(self.calls(), [])

    def test_switch_account_on_the_phone_is_the_device_code_sign_in_and_never_opens_a_browser(self):
        # Owner 11-27: "Switch account" tapped on the phone opened a browser on the Mac, which nobody there
        # could finish. It now shows the link and one-time code on the card, as "Add account" does.
        self.mode("hang")
        opened = []
        with mock.patch.object(cc, "open_page", side_effect=lambda url: opened.append(url) or True):
            self.assertEqual(self.tap("task-switch", self.ask("$card1"), "Switch account", "$card1"),
                             account_switch.ANSWERED)
        ((_, shown),) = self.posted("$card1")
        self.assertEqual((shown["id"], shown["state"]), (dl.DEVICE_CARD, "pending"))
        self.assertIn("open the sign-in page at auth.openai.com", shown["text"])
        self.assertEqual(metrics(self.work)[-1]["card"], "account-device-started")
        self.assertEqual(opened, [], "no browser on this computer")
        self.assertNotIn("on the computer", shown["text"], "never sent to the computer")
        self.assertTrue(self.dir.exists(), "the device sign-in is running")

    def test_a_device_code_that_cannot_start_leaves_the_sign_in_on_this_computer_one_tap_away(self):
        # Review of #6905: with "Switch account" as device code, the browser sign-in stays reachable by name.
        (self.user / ".codex").mkdir()
        (self.user / ".codex" / "stub-account").write_text("alice@example.com")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.user / ".codex")}):
            self.tap("task-switch", self.ask("$card1"), "Switch account", "$card1")
        ((_, refused),) = self.posted("$card1")
        self.assertEqual(refused["state"], "error")
        self.assertEqual(metrics(self.work)[-1]["card"], "account-not-started")
        labels = [b["label"] for b in refused["buttons"]]
        self.assertIn(account_switch.HERE_LABEL, labels, "the way that does not need device code")
        opened = []
        with mock.patch.object(cc, "open_page", side_effect=lambda url: opened.append(url) or True):
            self.tap("task-here", self.ask("$card2"), account_switch.HERE_LABEL, "$card2")
        self.assertEqual(metrics(self.work)[-1]["card"], "account-started")
        self.assertEqual(len(opened), 1, "the browser sign-in on this computer, asked for by name")

    def test_switch_account_on_this_computer_ends_a_device_sign_in(self):
        self.mode("hang")
        self.tap("task-add", self.ask("$card1"), "Add account", "$card1")
        self.posted("$card1")
        with mock.patch.object(cc, "SWITCH_ANYWHERE", False), mock.patch.object(cc, "open_page", return_value=True):
            self.tap("task-switch", self.ask("$card2"), "Switch account", "$card2")
        self.assertFalse(self.dir.exists())
        self.assertTrue(self.stub_gone())


class RemoteUnitTest(Home, unittest.TestCase):
    """What the card says where the connector's checks cannot be reached end to end."""

    OWNER = {"access_tier": "owner", "channel_kind": "dm", "user_id": "@alice:s.space", "channel_id": "!dm:s",
             "reply_to_event": "$e"}

    def remote(self, posted=True):
        self.said = []
        accounts = mock.Mock(name="accounts", publish=lambda path, body: self.said.append(body), quoted=lambda t: t,
                             pending=lambda: {}, post=mock.Mock(return_value=posted), using=lambda: "I'm using a.")
        accounts.name = "Codex"
        r = dl.Remote(accounts, self.state, Path(self.tmp.name), str(self.codex), self.env, lambda *a: None,
                      homes=lambda: [])
        r.fresh = lambda sent: True
        return r

    def queued(self, r):
        return [(c.kwargs["edit"], c.args[1][1]) for c in r.accounts.post.call_args_list]

    def test_a_new_add_after_an_ended_sign_in_withdraws_the_old_card(self):
        self.mode("fail")
        r = self.remote()
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$old"), "out"), "account-device-started")
        end = time.time() + 15
        while not (r.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(dl.current(r.dir), {}, "ended, never told: its card still shows the code")
        self.mode("hang")
        r.accounts.post.reset_mock()
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$new"), "out"), "account-device-started")
        queued = self.queued(r)
        self.assertEqual([(e, c["id"]) for e, c in queued], [("$new", dl.DEVICE_CARD), ("$old", dl.WITHDRAWN_CARD)])
        self.assertNotIn(CODE, json.dumps(queued[1][1]))
        self.assertFalse((self.state / dl.WITHDRAW_FILE).exists())

    def ended_on(self, r, event):
        """A sign-in shown on `event` that ended without being told: its card still shows the first code."""
        self.mode("fail")
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event=event), "out"), "account-device-started")
        end = time.time() + 15
        while not (r.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.05)
        self.mode("hang")
        r.accounts.post.reset_mock()

    def test_a_new_add_on_the_same_card_keeps_its_new_code_and_owes_nothing(self):
        r = self.remote()
        self.ended_on(r, "$e")
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$e"), "out"), "account-device-started")
        self.assertEqual([(e, c["id"]) for e, c in self.queued(r)], [("$e", dl.DEVICE_CARD)], "the live card stays")
        self.assertFalse((self.state / dl.WITHDRAW_FILE).exists(), "the old withdrawal is superseded")
        r.tick()
        self.assertEqual(len(self.queued(r)), 1, "and is never queued later")

    def test_a_new_add_on_the_same_card_whose_edit_fails_still_withdraws_the_first_code(self):
        r = self.remote()
        self.ended_on(r, "$e")
        r.accounts.post.return_value = False
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$e"), "out"), "account-not-started")
        self.assertEqual([o["event"] for o in dl.owed(r.dir)], ["$e"], "still owed")
        r.accounts.post.reset_mock()
        r.accounts.post.return_value = True
        self.assertEqual(r.tick(), "")
        self.assertEqual([(e, c["id"]) for e, c in self.queued(r)], [("$e", dl.WITHDRAWN_CARD)])
        self.assertFalse((self.state / dl.WITHDRAW_FILE).exists())

    def test_a_failed_edit_of_a_card_already_showing_the_code_keeps_it_and_the_sign_in(self):
        self.mode("hang")
        r = self.remote()
        r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$a"), "out")
        r.accounts.post.return_value = False
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$a"), "out"), "account-device-not-shown")
        self.assertEqual(r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$b"), "out"), "account-device-not-shown")
        meta = detached_login.read_json(r.dir / "meta.json")
        self.assertEqual((meta["cards"], meta["edit"]), (["$a"], "$a"), "still the live card, and the next word goes there")
        self.assertTrue(dl.current(r.dir), "the running sign-in goes on")
        self.assertEqual(self.said[-1], dl.NO_CARD_AGAIN + "\n")
        r.accounts.post.return_value = True
        r.stop()
        self.assertEqual([e for e, c in self.queued(r) if c["id"] == dl.WITHDRAWN_CARD], ["$a"])

    def test_the_card_is_on_record_before_its_code_is_queued(self):
        self.mode("hang")
        r = self.remote()
        seen = []
        r.accounts.post.side_effect = lambda room, built, edit="": seen.append(
            detached_login.read_json(r.dir / "meta.json").get("cards")) or (_ for _ in ()).throw(RuntimeError("stop"))
        with self.assertRaises(RuntimeError):
            r.tapped(dl.ADD, dict(self.OWNER, reply_to_event="$e"), "out")
        self.assertEqual(seen, [["$e"]], "recorded before the edit was queued")
        # Recovery: the sign-in is ended (here by a cancel), and its card is withdrawn.
        restarted = self.remote()
        restarted.stop()
        self.assertEqual([(e, c["id"]) for e, c in self.queued(restarted)], [("$e", dl.WITHDRAWN_CARD)])
        self.assertFalse((self.state / dl.WITHDRAW_FILE).exists())

    def test_a_tap_that_is_not_the_owners_in_their_dm_is_refused_here_too(self):
        r = self.remote()
        for sent in (dict(self.OWNER, access_tier="guest"), dict(self.OWNER, channel_kind="room")):
            self.assertEqual(r.tapped(dl.ADD, sent, "out"), "account-refused")
        self.assertEqual(self.said, [dl.NOT_OWNER + "\n"] * 2)
        self.assertEqual(self.calls(), [])

    def test_a_card_the_ledger_does_not_know_is_not_fresh(self):
        r = dl.Remote(mock.Mock(), self.state, Path(self.tmp.name), str(self.codex), self.env, lambda *a: None)
        self.assertFalse(r.fresh(self.OWNER))

    def test_where_no_card_can_be_shown_the_code_is_said_nowhere_and_the_sign_in_ends(self):
        self.mode("hang")
        r = self.remote(posted=False)
        self.assertEqual(r.tapped(dl.ADD, self.OWNER, "out"), "account-not-started")
        self.assertEqual(self.said, [dl.NO_CARD + "\n"])
        self.assertFalse((self.state / dl.STATE).exists())
        self.assertTrue(self.stub_gone())
        self.assertFalse(r.busy())

    def test_a_yes_that_fails_and_a_sign_out_that_fails_say_so(self):
        r = self.remote()
        with mock.patch.object(dl, "answer", return_value=("not-used", "why", ["$e"])):
            self.assertEqual(r.tapped(dl.USE, self.OWNER, "out"), "account-device-not-used")
        r.accounts.card.assert_called_with("error", "I could not use that account: why. I'm using a.", ask=True)
        with mock.patch.object(dl, "answer", return_value=("expired", "b@x.org", ["$old", "$e"])):
            self.assertEqual(r.tapped(dl.USE, self.OWNER, "out"), "account-device-expired")
        r.accounts.card.assert_called_with("error", dl.UNANSWERED % (5, "I'm using a."), ask=True)
        self.assertEqual([c.kwargs["edit"] for c in r.accounts.post.call_args_list], ["$e", "$e"], "the outcome, on the card answered")
        self.mode("logout-fail")
        self.assertEqual(r.tapped(dl.SIGN_OUT, self.OWNER, "out"), "account-not-signed-out")
        r.accounts.card.assert_called_with("error", "I could not sign out: codex logout ended with exit code 1.", ask=True)
        r.env = dict(self.env, CODEX_HOME="")
        self.assertEqual(r.tapped(dl.SIGN_OUT, self.OWNER, "out"), "account-not-signed-out")
        r.accounts.card.assert_called_with("error", "I could not sign out: %s." % dl.SHARED, ask=True)

    def test_a_withdrawal_that_cannot_be_queued_is_retried_on_the_next_upkeep(self):
        self.mode("fail")
        r = self.remote(posted=False)
        dl.start(str(self.codex), self.env, r.dir)
        dl.note_card(r.dir, "!dm:s", "$e1")
        dl.note_card(r.dir, "!dm:s", "$e2")
        end = time.time() + 15
        while not (r.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(r.tick(), "account-device-failed")
        record = self.state / dl.WITHDRAW_FILE
        self.assertEqual(oct(record.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(record.read_text()), {"owed": [
            {"room": "!dm:s", "event": "$e1", "kind": dl.WITHDRAWN_CARD},
            {"room": "!dm:s", "event": "$e2", "kind": dl.WITHDRAWN_CARD}]}, "no code or link kept")
        self.assertFalse(r.dir.exists(), "the sign-in itself is gone")
        self.assertEqual(r.tick(), "")
        self.assertTrue(record.exists(), "still owed while nothing can be queued")
        r.accounts.post.reset_mock(return_value=True)
        r.accounts.post.return_value = True
        self.assertEqual(r.tick(), "")
        self.assertFalse(record.exists())
        queued = [(c.args[0], c.kwargs["edit"], c.args[1][1]) for c in r.accounts.post.call_args_list]
        self.assertEqual([(room, edit, card["id"]) for room, edit, card in queued],
                         [("!dm:s", "$e1", dl.WITHDRAWN_CARD), ("!dm:s", "$e2", dl.WITHDRAWN_CARD)])
        self.assertFalse(any(CODE in json.dumps(card) or "auth.openai.com" in json.dumps(card) for *_, card in queued))
        self.assertEqual(r.tick(), "")
        self.assertEqual(len(r.accounts.post.call_args_list), 2, "each withdrawal queued once")

    def test_a_stop_after_the_sign_in_state_went_still_withdraws_its_cards(self):
        self.mode("fail")
        r = self.remote()
        dl.start(str(self.codex), self.env, r.dir)
        dl.note_card(r.dir, "!dm:s", "$e1")
        dl.note_card(r.dir, "!dm:s", "$e2")
        end = time.time() + 15
        while not (r.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.05)
        r.accounts.post.side_effect = RuntimeError("the process stops here")
        with self.assertRaises(RuntimeError):
            r.tick()
        self.assertFalse(r.dir.exists(), "the sign-in state, which named the cards, is gone")
        record = json.loads((self.state / dl.WITHDRAW_FILE).read_text())
        self.assertEqual([o["event"] for o in record["owed"]], ["$e1", "$e2"], "saved before it went")
        self.assertNotIn(CODE, json.dumps(record))
        restarted = self.remote()
        self.assertEqual(restarted.tick(), "")
        queued = [(c.kwargs["edit"], c.args[1][1]) for c in restarted.accounts.post.call_args_list]
        self.assertEqual([(e, card["id"]) for e, card in queued], [("$e1", dl.WITHDRAWN_CARD), ("$e2", dl.WITHDRAWN_CARD)])
        self.assertFalse(any(CODE in json.dumps(card) for _, card in queued))
        self.assertFalse((self.state / dl.WITHDRAW_FILE).exists())

    def test_an_outcome_whose_card_cannot_be_shown_is_logged(self):
        self.mode("fail")
        r = self.remote(posted=False)
        dl.start(str(self.codex), self.env, r.dir)
        dl.note_card(r.dir, "!dm:s", "$e")
        end = time.time() + 15
        while not (r.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(r.tick(), "account-device-failed")
        self.assertEqual(r.tick(), "")


class CardInvariantTest(Home, unittest.TestCase):
    """Every card ever queued with a code stays in exactly one of {the running sign-in's live cards, the
    withdrawal journal} until a code-free edit of it is queued, whatever happens in between: random,
    seeded sequences of taps, failed and working edits, finishes, expiries, cancels, answers and restarts."""

    # 8, 21 and 22 forgot a card still showing a code before the fix; all forty seeds 1-40 pass after it.
    SEEDS, STEPS, EVENTS = (1, 2, 8, 21, 22), 24, ("$a", "$b", "$c")
    OWNER = {"access_tier": "owner", "channel_kind": "dm", "user_id": "@alice:s.space", "channel_id": "!dm:s"}

    def remote(self):
        accounts = mock.Mock(name="accounts", publish=lambda path, body: None, quoted=lambda t: t, pending=lambda: {},
                             using=lambda: "I'm using a.", post=self.post,
                             card=lambda state, text, **kw: ("b", {"id": account_switch.CARD, "text": text}))
        accounts.name = "Codex"
        r = dl.Remote(accounts, self.state, Path(self.tmp.name), str(self.codex), self.env, lambda *a: None,
                      homes=lambda: [])
        r.fresh = lambda sent: True
        return r

    def post(self, room, built, edit=""):
        if not self.ok:
            return False
        if CODE in json.dumps(built[1]):
            self.shown.add(edit)
        else:
            self.shown.discard(edit)
        return True

    def check(self, step):
        meta = detached_login.read_json(self.dir / "meta.json")
        live = set(meta.get("cards") or []) if self.dir.exists() else set()
        owed = {o["event"] for o in dl.owed(self.dir)}
        for event in self.shown:
            self.assertTrue((event in live) != (event in owed),
                            "%s: %s live=%s owed=%s" % (step, event, sorted(live), sorted(owed)))

    def finish(self):
        """Let the running sign-in sign in now, and wait until it has ended."""
        if not (self.dir / "pids.json").exists():
            return
        (self.bin / "go").write_text("")
        end = time.time() + 10
        while self.dir.exists() and not (self.dir / "result.json").exists() and time.time() < end:
            time.sleep(0.02)
        (self.bin / "go").unlink()

    def test_no_card_with_a_code_is_ever_forgotten(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                rnd, self.shown, self.ok = random.Random(seed), set(), True
                self.mode("wait bob@example.org")
                r = self.remote()
                for step in range(self.STEPS):
                    op = rnd.choice(("add", "add", "add-fail", "finish", "tick", "tick-fail", "expire", "cancel",
                                     "yes", "no", "restart"))
                    event = rnd.choice(self.EVENTS)
                    self.ok = not op.endswith("-fail")
                    if op.startswith("add"):
                        r.tapped(dl.ADD, dict(self.OWNER, reply_to_event=event), "out")
                    elif op == "finish":
                        self.finish()
                        r.tick()
                    elif op.startswith("tick"):
                        r.tick()
                    elif op == "expire":
                        r.tick(now=time.time() + 10 * dl.LOGIN_WAIT_S)
                    elif op == "cancel":
                        r.stop()
                    elif op in ("yes", "no"):
                        target = detached_login.read_json(self.dir / "meta.json").get("edit") or event
                        r.tapped(dl.USE if op == "yes" else dl.DISCARD, dict(self.OWNER, reply_to_event=target), "out")
                    else:
                        r = self.remote()
                    self.check("seed %d step %d %s %s" % (seed, step, op, event))
                self.ok = True
                r.stop()
                r.retry()
                self.check("seed %d end" % seed)
                self.assertEqual(self.shown, set(), "every code card withdrawn in the end")
                if (self.home / "auth.json").exists():
                    (self.home / "auth.json").unlink()


if __name__ == "__main__":
    unittest.main()
