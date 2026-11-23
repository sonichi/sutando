#!/usr/bin/env python3
"""src/codex_account.py: the signed-in Codex account (email, plan), never a token. The auth.json is
synthetic (an unsigned JWT made here) and `codex` is a stub app-server that answers `account/read`
from a plan file."""
from __future__ import annotations

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
        print("not json"); print(json.dumps({"method": "noise"}))
        print(json.dumps({"id": m["id"], "result": {"userAgent": "stub"}}), flush=True)
    elif m["method"] == "account/read":
        if mode == "hang":
            time.sleep(60)
        if mode == "error":
            print(json.dumps({"id": m["id"], "error": {"message": "nope"}}), flush=True)
        elif mode == "exit":
            sys.exit(0)
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
        self.assertEqual({m["codex_home"] for m in sent}, {str(self.home)})
        rc, out = self.cli()
        self.assertEqual(out.strip(), f"Codex under {self.home} is signed in as app@example.com, pro plan, workspace Acme.")

    def test_an_app_server_that_errors_exits_hangs_or_is_missing_falls_back_to_the_file(self):
        self.auth({"email": "file@example.com", ca.PLAN_CLAIMS: {"chatgpt_plan_type": "plus"}})
        for mode in ("error", "exit", "gone"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"STUB_MODE": mode}):
                got = ca.read(self.home, str(self.codex))
                self.assertEqual((got["email"], got["plan"], got["source"], got["verified"]),
                                 ("file@example.com", "plus", "auth.json", False))
        with mock.patch.dict(os.environ, {"STUB_MODE": "hang"}):
            self.assertIsNone(ca.from_app_server(self.home, str(self.codex), timeout=1))
        self.assertEqual(ca.read(self.home, str(self.root / "no-such-codex"))["source"], "auth.json")

    def test_codex_saying_signed_out_wins_over_a_leftover_file(self):
        self.auth({"email": "stale@example.com"})
        with mock.patch.dict(os.environ, {"STUB_MODE": "signed-out"}):
            got = ca.read(self.home, str(self.codex))
            rc, out = self.cli()
        self.assertEqual((got["kind"], got["email"], got["verified"]), (None, None, True))
        self.assertEqual((rc, out.strip()), (1, f"Codex is not signed in under {self.home}."))
        self.assertNotIn("stale@example.com", out)

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

    def test_nothing_answering_and_no_file_is_unverified_signed_out(self):
        with mock.patch.dict(os.environ, {"STUB_MODE": "gone"}):
            rc, out = self.cli("--json")
        self.assertEqual((rc, json.loads(out)["kind"], json.loads(out)["verified"]), (1, None, None))

    def test_a_signed_out_answer_names_no_account(self):
        with mock.patch.dict(os.environ, {"STUB_MODE": "signed-out"}):
            self.assertEqual(ca.from_app_server(self.home, str(self.codex))["kind"], None)
            rc, out = self.cli()
        self.assertEqual((rc, out.strip()), (1, f"Codex is not signed in under {self.home}."))


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

    def test_no_file_or_a_broken_one_is_signed_out(self):
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
    def task(self, **headers):
        f = self.root / "task.txt"
        f.write_text("".join(f"{k}: {v}\n" for k, v in headers.items()) + "task: which account?\n")
        return f

    def gate(self, f):
        with mock.patch("owner_room_access.owner_routing", return_value={"owner_dm": "!dm:s"}):
            return ca.owner_dm_task(f, workspace=self.root)

    def test_only_the_owner_outside_shared_rooms_is_answered(self):
        self.assertIsNone(self.gate(self.task(access_tier="owner", channel_id="!dm:s")))
        self.assertIsNone(self.gate(self.task(access_tier="owner")))
        self.assertIn("not the owner's DM", self.gate(self.task(access_tier="owner", channel_id="!room:s")))
        self.assertIn("not the owner's", self.gate(self.task(access_tier="team", channel_id="!dm:s")))
        self.assertIn("cannot be read", self.gate(self.root / "missing.txt"))
        with mock.patch("owner_room_access.owner_routing", return_value={}):
            self.assertIn("not the owner's DM", ca.owner_dm_task(self.task(channel_id="!dm:s"), workspace=self.root))

    def test_a_refused_task_looks_nothing_up(self):
        self.auth({"email": "file@example.com"})
        f = self.task(access_tier="owner", channel_id="!room:s")
        with mock.patch("owner_room_access.owner_routing", return_value={"owner_dm": "!dm:s"}), \
                mock.patch("workspace_default.resolve_workspace", return_value=self.root):
            rc, out = self.cli("--task-file", str(f))
        self.assertEqual(rc, 3)
        self.assertNotIn("file@example.com", out)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
