#!/usr/bin/env python3
"""ms365.py — pure-logic coverage without the optional O365 dependency.

CI has no `O365` installed, so the module's import guard would sys.exit; we
inject a minimal fake `O365` module first, then the parser / credential-guard /
token-path / auth-guard logic runs for real (the live-Graph command bodies are
`# pragma: no cover`). Run: python3 skills/ms365-connect/scripts/ms365.test.py
"""
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

_MODULE_PATH = (Path(__file__).resolve().parent.parent
                / "skills" / "ms365-connect" / "scripts" / "ms365.py")


def _fake_o365():
    m = types.ModuleType("O365")
    class Account:  # minimal stand-in
        def __init__(self, *a, **k): self.is_authenticated = False
    class FileSystemTokenBackend:
        def __init__(self, *a, **k): pass
    m.Account = Account
    m.FileSystemTokenBackend = FileSystemTokenBackend
    return m


def _load():
    sys.modules["O365"] = _fake_o365()  # satisfy the import guard
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

    def test_defaults_to_cwd_state(self):
        os.environ.pop("MS365_STATE_DIR", None)
        self.assertTrue(ms365._token_dir().endswith(os.path.join("state", "ms365-token")))


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
