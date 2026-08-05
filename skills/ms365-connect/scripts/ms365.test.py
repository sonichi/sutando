#!/usr/bin/env python3
"""No-network tests for ms365.py.

Runnable directly:  python3 scripts/ms365.test.py

- Skips cleanly if the O365 dependency is not installed (import-guarded).
- Validates the argparse CLI parses every subcommand.
- Validates credential-checking exits cleanly (code 2) when creds are unset,
  without touching the network.
"""

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "ms365.py")


def _load_ms365():
    """Load the sibling ms365.py module by path.

    Returns the module, or None if the O365 dependency is missing (the module
    import-guards O365 and calls sys.exit(1) when it is absent).
    """
    spec = importlib.util.spec_from_file_location("ms365_under_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        # Top-level import guard fired: O365 not installed.
        return None
    except ImportError:
        return None
    return module


ms365 = _load_ms365()
_HAS_O365 = ms365 is not None


@unittest.skipUnless(_HAS_O365, "O365 not installed; skipping (pip install O365)")
class TestArgparse(unittest.TestCase):
    def setUp(self):
        self.parser = ms365.build_parser()

    def test_parses_each_subcommand(self):
        cases = [
            ["auth"],
            ["onedrive-list"],
            ["onedrive-list", "/Projects/Q3"],
            ["onedrive-get", "/Reports/x.pdf", "./x.pdf"],
            ["outlook-list"],
            ["outlook-list", "--n", "5"],
            ["outlook-send", "--to", "a@b.com", "--subject", "S", "--body", "B"],
            ["calendar-list"],
            ["calendar-list", "--days", "3"],
            ["teams-post", "--team", "Eng", "--channel", "General", "--message", "hi"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = self.parser.parse_args(argv)
                self.assertTrue(hasattr(args, "func"))
                self.assertTrue(callable(args.func))

    def test_defaults(self):
        args = self.parser.parse_args(["outlook-list"])
        self.assertEqual(args.n, 10)
        args = self.parser.parse_args(["calendar-list"])
        self.assertEqual(args.days, 7)
        args = self.parser.parse_args(["onedrive-list"])
        self.assertIsNone(args.folder)

    def test_missing_subcommand_errors(self):
        # argparse exits (code 2) when no subcommand is given.
        with self.assertRaises(SystemExit):
            self.parser.parse_args([])

    def test_required_flags_enforced(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["outlook-send", "--to", "a@b.com"])


@unittest.skipUnless(_HAS_O365, "O365 not installed; skipping (pip install O365)")
class TestCredentialGuard(unittest.TestCase):
    def setUp(self):
        # Snapshot and clear the credential env so no network is possible.
        self._saved = {}
        for name in ("MS365_CLIENT_ID", "MS365_CLIENT_SECRET", "MS365_TENANT_ID"):
            self._saved[name] = os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_require_credentials_exits_2_when_unset(self):
        with self.assertRaises(SystemExit) as ctx:
            ms365._require_credentials()
        self.assertEqual(ctx.exception.code, 2)

    def test_command_handler_exits_before_network(self):
        # cmd_onedrive_list -> _build_account -> _require_credentials exits(2)
        # before any O365/network call is attempted.
        parser = ms365.build_parser()
        args = parser.parse_args(["onedrive-list"])
        with self.assertRaises(SystemExit) as ctx:
            args.func(args)
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    if not _HAS_O365:
        sys.stderr.write("O365 not installed; tests skipped.\n")
    unittest.main(verbosity=2)
