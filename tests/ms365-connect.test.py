#!/usr/bin/env python3
"""ms365.py — pure-logic coverage without the optional O365 dependency.

O365 is imported lazily (only inside `_build_account`, a `# pragma: no cover`
live-Graph path), so the module imports cleanly on any interpreter WITHOUT
`O365` installed — which is exactly the CI environment. We deliberately do NOT
inject a fake `O365` here: a clean import is part of what this suite pins (the
fix that makes `ms365.py --help` work on Python 3.9, where O365 can't import).
The parser / credential-guard / token-path / auth-guard / main-dispatch logic
runs for real. Run: python3 tests/ms365-connect.test.py
"""
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

_MODULE_PATH = (Path(__file__).resolve().parent.parent
                / "skills" / "ms365-connect" / "scripts" / "ms365.py")


def _load():
    # No fake O365 injected on purpose — the module must import without it.
    sys.modules.pop("O365", None)
    spec = importlib.util.spec_from_file_location("ms365_under_test", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms365 = _load()


class TestParser(unittest.TestCase):
    def test_each_subcommand_parses(self):
        p = ms365.build_parser()
        for argv in (["auth"], ["onedrive-list"], ["onedrive-get", "a", "b"],
                     ["outlook-list"], ["outlook-send", "--to", "x@y.z", "--subject", "s", "--body", "b"],
                     ["calendar-list"], ["teams-post", "--team", "t", "--channel", "c", "--message", "m"]):
            ns = p.parse_args(argv)
            self.assertTrue(hasattr(ns, "func") or hasattr(ns, "command") or ns is not None)

    def test_no_subcommand_errors(self):
        p = ms365.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args([])


class TestCredentialGuard(unittest.TestCase):
    def setUp(self):
        for k in ("MS365_CLIENT_ID", "MS365_CLIENT_SECRET", "MS365_TENANT_ID"):
            os.environ.pop(k, None)

    def test_require_credentials_exits_2_when_unset(self):
        with self.assertRaises(SystemExit) as cm:
            ms365._require_credentials()
        self.assertEqual(cm.exception.code, 2)

    def test_require_credentials_returns_tuple_when_set(self):
        os.environ.update(MS365_CLIENT_ID="id", MS365_CLIENT_SECRET="sec", MS365_TENANT_ID="ten")
        self.assertEqual(ms365._require_credentials(), ("id", "sec", "ten"))

    def test_public_flow_does_not_require_secret(self):
        # An app registered under "Mobile and desktop applications" is a PUBLIC
        # client: the exchange must not present a secret (AADSTS700025, live
        # 2026-08-06) — so the guard must not demand one either.
        os.environ.update(MS365_CLIENT_ID="id", MS365_TENANT_ID="ten",
                          MS365_AUTH_FLOW="public")
        try:
            self.assertEqual(ms365._require_credentials(), ("id", None, "ten"))
        finally:
            os.environ.pop("MS365_AUTH_FLOW", None)

    def test_default_flow_still_requires_secret(self):
        os.environ.update(MS365_CLIENT_ID="id", MS365_TENANT_ID="ten")
        with self.assertRaises(SystemExit) as cm:
            ms365._require_credentials()
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_flow_exits_2(self):
        os.environ.update(MS365_CLIENT_ID="id", MS365_TENANT_ID="ten",
                          MS365_AUTH_FLOW="pkce")
        try:
            with self.assertRaises(SystemExit) as cm:
                ms365._auth_flow()
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.environ.pop("MS365_AUTH_FLOW", None)


class TestTokenDir(unittest.TestCase):
    def test_honors_state_dir_env(self):
        os.environ["MS365_STATE_DIR"] = "/tmp/ms365state"
        try:
            self.assertTrue(ms365._token_dir().startswith("/tmp/ms365state"))
            self.assertTrue(ms365._token_dir().endswith("ms365-token"))
        finally:
            os.environ.pop("MS365_STATE_DIR", None)

    def test_fail_closed_when_workspace_unresolvable(self):
        # No MS365_STATE_DIR + resolver raises => exit, never a silent
        # os.getcwd()/state fallback that would strand the token (CR #2682).
        os.environ.pop("MS365_STATE_DIR", None)
        fake = types.ModuleType("workspace_default")
        def _raise(migrate=True):
            raise RuntimeError("no workspace here")
        fake.resolve_workspace = _raise
        saved = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = fake
        try:
            with self.assertRaises(SystemExit):
                ms365._token_dir()
        finally:
            if saved is not None:
                sys.modules["workspace_default"] = saved
            else:
                sys.modules.pop("workspace_default", None)

    def test_defaults_to_resolved_workspace_state(self):
        # Without MS365_STATE_DIR, the token dir defaults through the repo's
        # resolve_workspace() (an absolute path under the resolved workspace),
        # NOT a bare os.getcwd()/state join — repo root and workspace differ on
        # some hosts, so cwd-relative caching would land the token in the wrong
        # tree. We assert the shape (absolute + .../state/ms365-token) rather
        # than a literal path, since the resolved workspace is host-specific.
        os.environ.pop("MS365_STATE_DIR", None)
        td = ms365._token_dir()
        self.assertTrue(os.path.isabs(td), td)
        self.assertTrue(td.endswith(os.path.join("state", "ms365-token")), td)


class TestMain(unittest.TestCase):
    def test_main_dispatches_to_subcommand_func(self):
        # main() must build the parser, parse argv, and call the selected
        # subcommand's func — patch a handler and confirm it's invoked with the
        # parsed namespace and its return value propagates.
        seen = {}

        def fake(args):
            seen["n"] = args.n
            return 0

        orig = ms365.cmd_outlook_list
        ms365.cmd_outlook_list = fake  # build_parser (called inside main) binds this
        try:
            rc = ms365.main(["outlook-list", "--n", "3"])
        finally:
            ms365.cmd_outlook_list = orig
        self.assertEqual(rc, 0)
        self.assertEqual(seen.get("n"), 3)


class TestScopes(unittest.TestCase):
    def test_no_msal_reserved_scopes(self):
        # MSAL reserves openid/offline_access/profile and adds them itself;
        # passing them explicitly raises ValueError and breaks `auth`.
        reserved = {"openid", "offline_access", "profile"}
        overlap = reserved.intersection(s.lower() for s in ms365.SCOPES)
        self.assertEqual(overlap, set(), f"reserved scopes leaked: {overlap}")

    def test_teams_lookup_scopes_present(self):
        # qingyun CR #2682: teams-post enumerates the team (/me/joinedTeams) and
        # channel (/teams/{id}/channels) by name BEFORE it can send. Graph needs
        # delegated Team.ReadBasic.All + Channel.ReadBasic.All for those lookups;
        # ChannelMessage.Send/Chat.Read do NOT cover them. Pin both so a future
        # edit can't drop a lookup scope and reintroduce the "Team not found" trap.
        self.assertEqual(ms365.TEAMS_LOOKUP_SCOPES,
                         ("Team.ReadBasic.All", "Channel.ReadBasic.All"))
        for scope in ms365.TEAMS_LOOKUP_SCOPES:
            self.assertIn(scope, ms365.SCOPES)

    def test_teams_lookup_scopes_documented_in_setup_guide(self):
        # The consent guide the user follows must list every scope teams-post
        # needs, or they grant an incomplete set and the lookups fail silently.
        skill_md = (Path(_MODULE_PATH).resolve().parents[1] / "SKILL.md").read_text()
        for scope in ms365.TEAMS_LOOKUP_SCOPES:
            self.assertIn(scope, skill_md, f"{scope} missing from SKILL.md consent guide")


class TestTokenSecurity(unittest.TestCase):
    """The cached OAuth token grants Mail/Files/Calendar access — its dir must be
    0700 and the token file 0600, even though O365's backend leaves them looser."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_secure_dir_forces_owner_only_0700(self):
        d = os.path.join(self.tmp, "ms365-token")
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o755)  # start world-readable, as makedirs default would
        ms365._secure_dir(d)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_restrict_token_file_forces_0600(self):
        d = ms365._secure_dir(os.path.join(self.tmp, "t"))
        p = os.path.join(d, "o365_token.txt")
        with open(p, "w") as f:
            f.write("refresh-token")
        os.chmod(p, 0o644)  # what FileSystemTokenBackend leaves it as
        ms365._restrict_token_file(d)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_owner_only_backend_chmods_token_after_every_save(self):
        d = ms365._secure_dir(os.path.join(self.tmp, "b"))
        token_path = os.path.join(d, "o365_token.txt")

        class _FakeBackend:  # stand-in for O365's FileSystemTokenBackend
            def __init__(self, token_path=None, token_filename=None):
                self.dir, self.name = token_path, token_filename

            def save_token(self, force=False):
                fp = os.path.join(self.dir, self.name)
                with open(fp, "w") as f:
                    f.write("refresh-token")
                os.chmod(fp, 0o644)  # the base leaves it world-readable
                return True

        backend = ms365._owner_only_backend(_FakeBackend, d)
        backend.save_token()
        self.assertEqual(stat.S_IMODE(os.stat(token_path).st_mode), 0o600)


class TestAuthGuard(unittest.TestCase):
    def test_ensure_authenticated_exits_3_when_not_authed(self):
        acct = types.SimpleNamespace(is_authenticated=False)
        with self.assertRaises(SystemExit) as cm:
            ms365._ensure_authenticated(acct)
        self.assertEqual(cm.exception.code, 3)

    def test_ensure_authenticated_passes_when_authed(self):
        acct = types.SimpleNamespace(is_authenticated=True)
        ms365._ensure_authenticated(acct)  # no raise


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if res.wasSuccessful() else 1)
