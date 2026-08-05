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
import sys
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


class TestTokenDir(unittest.TestCase):
    def test_honors_state_dir_env(self):
        os.environ["MS365_STATE_DIR"] = "/tmp/ms365state"
        try:
            self.assertTrue(ms365._token_dir().startswith("/tmp/ms365state"))
            self.assertTrue(ms365._token_dir().endswith("ms365-token"))
        finally:
            os.environ.pop("MS365_STATE_DIR", None)

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
