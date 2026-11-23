#!/usr/bin/env python3
"""src/codex_account.py: the signed-in Codex account (email, plan), never a token. The auth.json is
synthetic (an unsigned JWT made here) and `codex` is a stub app-server that answers `account/read`
from a plan file."""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import codex_account as ca  # noqa: E402

SECRET = "sk-synthetic-never-printed"
ACCESS = "access-synthetic-never-printed"
REFRESH = "refresh-synthetic-never-printed"

STUB = r'''
import json, os, sys, time
mode = os.environ.get("STUB_MODE", "ok")
if mode == "gone":
    sys.exit(0)
log = os.environ.get("STUB_LOG")
for line in sys.stdin:
    m = json.loads(line)
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"method": m.get("method"), "codex_home": os.environ.get("CODEX_HOME")}) + "\n")
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        open(os.path.join(os.environ["CODEX_HOME"], "state.sqlite"), "w").write("touched")
        print("not json"); print(json.dumps({"method": "noise"}))
        print(json.dumps({"id": m["id"], "result": {"userAgent": "stub"}}), flush=True)
    elif m["method"] == "account/read":
        if mode == "hang":
            time.sleep(60)
        if mode == "error":
            print(json.dumps({"id": m["id"], "error": {"message": "nope"}}), flush=True)
        elif mode == "exit":
            sys.exit(0)
        elif mode == "malformed":
            print(json.dumps({"id": m["id"], "result": {"account": {"email": "x@example.com"}}}), flush=True)
        elif mode == "no-result":
            print(json.dumps({"id": m["id"], "result": {"requiresOpenaiAuth": True}}), flush=True)
        elif mode == "signed-out":
            print(json.dumps({"id": m["id"], "result": {"account": None, "requiresOpenaiAuth": True}}), flush=True)
        else:
            print(json.dumps({"id": m["id"], "result": {"account": {"type": "chatgpt", "email": "app@example.com",
                  "planType": "pro", "workspaceName": "Acme"}, "requiresOpenaiAuth": True}}), flush=True)
'''


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        stub = self.root / "stub.py"
        stub.write_text(STUB)
        self.codex = self.root / "codex"
        self.codex.write_text(f'#!/bin/bash\n[ "$1 $2 $3" = "app-server --listen stdio://" ] || exit 9\nexec "{sys.executable}" "{stub}"\n')
        self.codex.chmod(0o755)
        self.log = self.root / "stub.log"
        env = mock.patch.dict(os.environ, {"STUB_LOG": str(self.log), "STUB_MODE": "ok"})
        env.start()
        self.addCleanup(env.stop)

    def auth(self, claims=None, api_key=None, raw=None):
        if raw is not None:
            (self.home / "auth.json").write_text(raw)
            return
        data = {"OPENAI_API_KEY": api_key, "last_refresh": "2026-11-22T00:00:00Z"}
        if claims is not None:
            data["tokens"] = {"id_token": jwt(claims), "access_token": ACCESS, "refresh_token": REFRESH,
                              "account_id": "acct-synthetic"}
        (self.home / "auth.json").write_text(json.dumps(data))

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ca.main(["--codex-home", str(self.home), "--codex", str(self.codex), *argv])
        return rc, out.getvalue()

    def assertNoSecrets(self, text):
        for s in (SECRET, ACCESS, REFRESH, "acct-synthetic", "eyJ"):
            self.assertNotIn(s, text)


class AppServer(Case):
    def test_codex_own_answer_is_used_under_the_given_home(self):
        self.auth({"email": "file@example.com"})
        rc, out = self.cli("--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"codex_home": str(self.home), "email": "app@example.com", "plan": "pro",
                                           "workspace": "Acme", "kind": "chatgpt", "source": "app-server",
                                           "verified": True})
        sent = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "account/read"])
        seen = {m["codex_home"] for m in sent}
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen, {str(self.home)}, "Codex runs on a copy, never in the core's home")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["auth.json"], "the home is not written")
        self.assertFalse(Path(seen.pop()).exists(), "the copy is removed")
        rc, out = self.cli()
        self.assertEqual(out.strip(), f"Codex under {self.home} is signed in as app@example.com, pro plan, workspace Acme.")

    def test_an_app_server_that_errors_exits_hangs_or_is_missing_falls_back_to_the_file(self):
        self.auth({"email": "file@example.com", ca.PLAN_CLAIMS: {"chatgpt_plan_type": "plus"}})
        for mode in ("error", "exit", "gone", "malformed", "no-result", "signed-out"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"STUB_MODE": mode}):
                got = ca.read(self.home, str(self.codex))
                self.assertEqual((got["email"], got["plan"], got["source"], got["verified"]),
                                 ("file@example.com", "plus", "auth.json", False))
        with mock.patch.dict(os.environ, {"STUB_MODE": "hang"}):
            self.assertIsNone(ca.from_app_server(self.home, str(self.codex), timeout=1))
        self.assertEqual(ca.read(self.home, str(self.root / "no-such-codex"))["source"], "auth.json")

    def test_a_bare_codex_name_is_found_where_the_launcher_looks(self):
        bindir = self.root / "npm-bin"
        app = self.root / "app"
        (app / "cli" / "codex" / "bin").mkdir(parents=True)
        bindir.mkdir()
        for where, extra in ((bindir, {"CODEX_INSTALL_BIN": str(bindir)}),
                             (app / "cli" / "codex" / "bin", {"SUTANDO_APP_SUPPORT": str(app)})):
            with self.subTest(where=where.name):
                for d in (bindir, app / "cli" / "codex" / "bin"):
                    (d / "codex").unlink(missing_ok=True)
                (where / "codex").symlink_to(self.codex)
                env = {"PATH": "/usr/bin:/bin", "CODEX_NODE_BIN": str(self.root / "no-node"),
                       "CODEX_INSTALL_BIN": str(self.root / "none"), "SUTANDO_APP_SUPPORT": "", **extra}
                with mock.patch.dict(os.environ, env):
                    parts = ca.launch_path().split(os.pathsep)
                    got = ca.from_app_server(self.home, "codex")
                self.assertEqual(parts[:2], ["/usr/bin", "/bin"])
                self.assertIn(os.path.realpath(where), [os.path.realpath(p) for p in parts])
                self.assertEqual((got["email"], got["source"], got["verified"]), ("app@example.com", "app-server", True))

    def test_a_relative_codex_home_is_resolved_before_spawning(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            self.auth({"email": "file@example.com"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ca.main(["--codex-home", "codex-home", "--codex", str(self.codex), "--json"])
        finally:
            os.chdir(cwd)
        self.assertEqual(os.path.realpath(json.loads(out.getvalue())["codex_home"]), os.path.realpath(self.home))

    def test_a_null_from_the_copy_is_unknown_and_the_file_decides(self):
        self.auth({"email": "kept@example.com"})
        with mock.patch.dict(os.environ, {"STUB_MODE": "signed-out"}):
            self.assertIsNone(ca.from_app_server(self.home, str(self.codex)))
            got = ca.read(self.home, str(self.codex))
        self.assertEqual((got["email"], got["verified"]), ("kept@example.com", False))

    def test_no_account_anywhere_is_none_found_not_signed_out(self):
        with mock.patch.dict(os.environ, {"STUB_MODE": "signed-out"}):
            rc, out = self.cli("--json")
            rc2, text = self.cli()
        self.assertEqual((rc, json.loads(out)["kind"], json.loads(out)["verified"]), (1, None, None))
        self.assertEqual((rc2, text.strip()), (1, f"I found no sign-in for Codex under {self.home}."))

    def test_nothing_answering_and_no_file_is_none_found(self):
        with mock.patch.dict(os.environ, {"STUB_MODE": "gone"}):
            rc, out = self.cli("--json")
        self.assertEqual((rc, json.loads(out)["kind"], json.loads(out)["verified"]), (1, None, None))

    def test_a_failure_while_preparing_the_copy_leaves_nothing_behind(self):
        self.auth({"email": "file@example.com"})
        made = []
        real = ca.tempfile.mkdtemp

        def mkdtemp(**kw):
            made.append(real(**kw))
            return made[-1]

        def copy_then_fail(scratch, home):
            (Path(scratch) / "auth.json").write_text("copy")
            raise OSError("disk full")
        with mock.patch.object(ca.tempfile, "mkdtemp", mkdtemp), mock.patch.object(ca, "_copy_into", copy_then_fail):
            self.assertIsNone(ca.from_app_server(self.home, str(self.codex)))
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists(), "the partial copy is removed")
        self.assertFalse(self.log.exists(), "no Codex was started")

    def test_a_failure_starting_codex_leaves_no_copy(self):
        self.auth({"email": "file@example.com"})
        made = []
        real = ca.tempfile.mkdtemp

        def mkdtemp(**kw):
            made.append(real(**kw))
            return made[-1]
        with mock.patch.object(ca.tempfile, "mkdtemp", mkdtemp):
            self.assertIsNone(ca.from_app_server(self.home, str(self.root / "no-such-codex")))
        self.assertFalse(Path(made[0]).exists())


class Envelope(unittest.TestCase):
    """`account_of`: only a successful response naming an account with a type is an answer."""

    def test_an_account_with_a_type_is_the_answer(self):
        acct = {"type": "chatgpt", "email": "a@example.com"}
        self.assertEqual(ca.account_of({"id": 2, "result": {"account": acct}}), acct)

    def test_an_explicit_null_account_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": {"account": None, "requiresOpenaiAuth": True}}))

    def test_an_error_response_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "error": {"message": "nope"}, "result": {"account": {"type": "x"}}}))

    def test_a_response_without_result_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2}))

    def test_a_result_that_is_not_an_object_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": "account"}))

    def test_a_result_without_account_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": {"requiresOpenaiAuth": True}}))

    def test_an_account_without_a_type_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": {"account": {"email": "a@example.com"}}}))

    def test_an_account_with_an_empty_type_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": {"account": {"type": ""}}}))

    def test_an_account_that_is_not_an_object_is_no_answer(self):
        self.assertIsNone(ca.account_of({"id": 2, "result": {"account": "chatgpt"}}))

    def test_an_envelope_that_is_not_an_object_is_no_answer(self):
        self.assertIsNone(ca.account_of(None))
        self.assertIsNone(ca.account_of([]))


class AuthFile(Case):
    def test_only_email_and_plan_claims_are_read_and_no_token_is_printed(self):
        self.auth({"email": "file@example.com", "sub": "secret-sub", ca.PLAN_CLAIMS: {"chatgpt_plan_type": "plus",
                   "chatgpt_account_id": "acct-synthetic"}}, api_key=SECRET)
        rc, out = self.cli("--no-app-server", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"codex_home": str(self.home), "email": "file@example.com", "plan": "plus",
                                           "workspace": None, "kind": "chatgpt", "source": "auth.json",
                                           "verified": False})
        self.assertNoSecrets(out)
        self.assertNotIn("secret-sub", out)
        self.assertFalse(self.log.exists(), "--no-app-server starts no Codex")

    def test_an_id_token_kept_parsed_is_read_by_its_raw_jwt(self):
        (self.home / "auth.json").write_text(json.dumps({"tokens": {"id_token": {"raw_jwt": jwt({"email": "p@example.com"})}}}))
        got = ca.from_auth_file(self.home)
        self.assertEqual((got["email"], got["plan"]), ("p@example.com", None))
        self.assertEqual(ca.words(got, self.home), f"Codex under {self.home} is signed in as p@example.com (from its saved sign-in file; "
                         "Codex itself did not confirm it).")

    def test_an_api_key_login_says_so_without_the_key(self):
        self.auth(api_key=SECRET)
        rc, out = self.cli("--no-app-server")
        self.assertEqual((rc, out.strip()), (0, f"Codex under {self.home} uses an API key (no ChatGPT account) (from its saved sign-in file)."))
        self.assertNoSecrets(out)

    def test_no_file_or_a_broken_one_names_no_account(self):
        self.assertIsNone(ca.from_auth_file(self.home))
        for raw in ("{", "[]", json.dumps({"tokens": {"id_token": "not-a-jwt"}}), json.dumps({"tokens": "x"})):
            with self.subTest(raw=raw):
                self.auth(raw=raw)
                self.assertIsNone(ca.from_auth_file(self.home))
        rc, out = self.cli("--no-app-server", "--json")
        self.assertEqual((rc, json.loads(out)["kind"]), (1, None))

    def test_an_account_with_no_email_is_named_as_such(self):
        self.assertEqual(ca.words({"kind": "chatgpt", "verified": True}, self.home),
                         f"Codex under {self.home} is signed in as an account whose email Codex does not report.")


class Home(Case):
    def test_the_launchers_effective_home_is_used_and_its_refusal_kept(self):
        import codex_home
        with mock.patch.object(codex_home, "effective", return_value=str(self.root / "cfg")) as eff:
            self.assertEqual(ca.home(REPO), self.root / "cfg")
        self.assertEqual(eff.call_args.args[0], REPO)
        with mock.patch.object(codex_home, "effective", side_effect=codex_home.Refused("not a CODEX_HOME")):
            with self.assertRaisesRegex(ca.NoHome, "not a CODEX_HOME"):
                ca.home(REPO)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ca.main(["--no-app-server"])
        self.assertEqual((rc, out.getvalue().strip()), (1, "No Codex home to read: not a CODEX_HOME."))
        with mock.patch.object(codex_home, "effective", side_effect=RuntimeError("unreadable")):
            with self.assertRaisesRegex(ca.NoHome, "cannot be read"):
                ca.home(REPO)

    def test_without_codex_home_the_cores_home_is_read(self):
        self.auth({"email": "core@example.com"})
        out = io.StringIO()
        with mock.patch.object(ca, "home", return_value=self.home), contextlib.redirect_stdout(out):
            rc = ca.main(["--no-app-server", "--json"])
        self.assertEqual((rc, json.loads(out.getvalue())["email"]), (0, "core@example.com"))


class OwnerOnly(Case):
    OWNER, DM = "@owner:s.space", "!dm:s.space"

    def task(self, mid=True, **headers):
        base = {"access_tier": "owner", "user_id": self.OWNER, "channel_id": self.DM, "channel_kind": "dm"}
        base.update(headers)
        lines = [f"{k}: {v}" for k, v in base.items() if v is not None]
        if mid:
            text = "id: t1\ntask_layout: mid\ntask: which account?\n" + "\n".join(lines) + "\n"
        else:
            text = "\n".join(lines) + "\ntask: which account?\n"
        f = self.root / "task.txt"
        f.write_text(text)
        return f

    def gate(self, f, rows=None, dm=None):
        import connections
        rows = [{"instance": "s_dspace", "owner_mxid": self.OWNER}] if rows is None else rows
        with mock.patch.object(connections, "read_rows", return_value=rows), \
                mock.patch.object(connections, "owner_dm", return_value=self.DM if dm is None else dm):
            return ca.owner_dm_task(f, workspace=self.root)

    def test_only_an_attested_owner_task_from_the_checked_dm_is_answered(self):
        self.assertIsNone(self.gate(self.task()))
        self.assertIsNone(self.gate(self.task(mid=False)))

    def test_everything_else_fails_closed(self):
        cases = {
            "no tier": self.task(access_tier=None),
            "team": self.task(access_tier="team"),
            "collaborator": self.task(collaborator="true"),
            "another sender": self.task(user_id="@other:s.space"),
            "a room": self.task(channel_id="!room:s.space", channel_kind="group"),
            "no room": self.task(channel_id=None, channel_kind=None),
        }
        for name, f in cases.items():
            with self.subTest(case=name):
                self.assertIsNotNone(self.gate(f))
        self.assertIsNotNone(self.gate(self.task(), rows=[]))
        self.assertIsNotNone(self.gate(self.task(), dm=""))
        self.assertIsNotNone(self.gate(self.root / "missing.txt"))

    def test_a_body_cannot_forge_the_tier_of_a_task_last_file(self):
        f = self.root / "task.txt"
        f.write_text(f"user_id: {self.OWNER}\ntask: hi\naccess_tier: owner\nchannel_id: {self.DM}\nchannel_kind: dm\n")
        self.assertIsNotNone(self.gate(f))

    def test_a_refused_task_looks_nothing_up(self):
        self.auth({"email": "file@example.com"})
        f = self.task(channel_id="!room:s.space", channel_kind="group")
        import connections
        import workspace_default
        with mock.patch.object(connections, "read_rows", return_value=[{"owner_mxid": self.OWNER}]), \
                mock.patch.object(connections, "owner_dm", return_value=self.DM), \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=self.root):
            rc, out = self.cli("--task-file", str(f))
        self.assertEqual(rc, 3)
        self.assertNotIn("file@example.com", out)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
