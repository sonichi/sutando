#!/usr/bin/env python3
"""One optional probe's import must not be able to take down all 39 checks.

`check_quota_account_identity` (added #2547) reads a launchd plist, so it needs
`plistlib`. That import sat at MODULE scope, where an ImportError is unreachable
by any handler and kills the process before a single check runs.

`plistlib` is not an ordinary stdlib import: it pulls in `xml.parsers.expat` ->
the `pyexpat` C extension, which dlopens libexpat. A Python whose pyexpat was
built against a different libexpat than it finds at runtime raises ImportError.
Measured on a live host 2026-08-03 — same file, same commit, two interpreters:

    /opt/homebrew/bin/python3 3.14.5 -> ImportError: dlopen pyexpat, symbol
                                        _XML_SetAllocTrackerActivationThreshold
                                        not found in /usr/lib/libexpat.1.dylib
                                        ->  0 of 39 checks ran
    /usr/bin/python3          3.9.6  -> 39 of 39 checks ran

So the health check — the one tool whose job is to notice things being down —
was itself silently down on that interpreter, for one optional probe on one
platform. PR #2582 fixes the installer half (choose an interpreter that works);
this is the code half (survive one that does not).

`test_module_imports_when_plistlib_is_unavailable` is the load-bearing case: it
FAILS on the parent commit. `test_the_blocker_actually_blocks` is the control —
without it, a blocker that silently no-opped would make every other case here
pass for the wrong reason, and a check that cannot produce a positive is not
evidence of a negative.

Deliberately: these cases never import plistlib themselves, so this file runs to
completion on exactly the broken interpreter it describes. Only the
no-regression case needs a real plist, and it skips when plistlib is missing.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
HEALTH_CHECK = REPO / "src" / "health-check.py"


class _BlockPlistlib:
    """Make `import plistlib` raise ImportError, as a broken pyexpat does.

    Simulated rather than reproduced: reproducing it needs a mismatched libexpat,
    which is a property of the host, not something a test can arrange. The symptom
    is what the production code has to survive, and the symptom is exactly this.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "plistlib" or fullname.startswith("plistlib."):
            raise ImportError(
                "dlopen(pyexpat.cpython-314-darwin.so): Symbol not found: "
                "_XML_SetAllocTrackerActivationThreshold (simulated)"
            )
        return None

    def __enter__(self):
        self._saved = sys.modules.pop("plistlib", None)
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *exc):
        sys.meta_path.remove(self)
        if self._saved is not None:
            sys.modules["plistlib"] = self._saved
        return False


def _load_health_check(module_name):
    """Load health-check.py under a fresh name.

    A fresh name per load matters: a cached `sys.modules` entry would let the
    module appear to import under the blocker when it had really been imported
    earlier, unblocked — the module-scope import would never be re-executed and
    the load-bearing case could not fail even on the parent commit.
    """
    spec = importlib.util.spec_from_file_location(module_name, HEALTH_CHECK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return mod


class TestBlockerControl(unittest.TestCase):
    def test_the_blocker_actually_blocks(self):
        """The control. If this cannot go positive, nothing below means anything."""
        with _BlockPlistlib():
            with self.assertRaises(ImportError):
                import plistlib  # noqa: F401

    def test_the_blocker_leaves_the_rest_of_the_stdlib_alone(self):
        """An over-broad blocker would make the cases below prove something else
        (e.g. 'health-check imports with half the stdlib gone')."""
        with _BlockPlistlib():
            import json  # noqa: F401
            import subprocess  # noqa: F401

    def test_plistlib_is_restored_afterwards(self):
        """The blocker must not leak into sibling tests in the same process."""
        with _BlockPlistlib():
            pass
        import plistlib
        self.assertTrue(hasattr(plistlib, "loads"))


class TestImportWithoutPlistlib(unittest.TestCase):
    def test_module_imports_when_plistlib_is_unavailable(self):
        """THE pin. On the parent commit this raises ImportError at module scope
        and every one of the 39 checks is lost with it."""
        with _BlockPlistlib():
            mod = _load_health_check("health_check_no_plistlib")
        self.assertTrue(
            hasattr(mod, "check_quota_account_identity"),
            "health-check.py must import on a Python that cannot import plistlib",
        )

    def test_the_other_checks_are_still_defined(self):
        """Importing is only worth anything if the checks came with it — a stub
        that imported and defined nothing would satisfy the case above."""
        with _BlockPlistlib():
            mod = _load_health_check("health_check_no_plistlib_2")
        defined = [n for n in dir(mod) if n.startswith("check_")]
        self.assertGreater(
            len(defined), 20,
            f"expected the full check set to survive the import, found {len(defined)}",
        )


class TestProbeDegradesAlone(unittest.TestCase):
    """The probe that needs plistlib must warn for itself, not raise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self.plist = self.home / "Library/LaunchAgents/com.sutando.credential-proxy.plist"

    def _run(self, mod):
        """Reach the plistlib import: proxy up, core routed, config dir set, plist
        present. Routing is stated explicitly rather than inherited from the host
        — a developer machine whose core IS proxy-routed would otherwise pass
        these for the wrong reason.

        The listener's environment is pinned UNREADABLE for the same reason.
        Since #2896 the running process is consulted before the plist, so a
        readable one answers first and the plist path — the only path that
        needs plistlib — is never reached: these cases stopped exercising what
        they exist to guard, and read the developer's live proxy while doing it.
        """
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CONFIG_DIR": "/tmp/x/.claude-sutando",
                              "ANTHROPIC_BASE_URL": "http://localhost:7846"},
                             clear=False), \
             mock.patch.object(mod.Path, "home", staticmethod(lambda: self.home)), \
             mock.patch.object(mod, "_proxy_config_dir_from_process",
                               return_value=mod._PROXY_ENV_UNREADABLE), \
             mock.patch.object(mod, "_runtime_may_skip_proxy", return_value=False):
            return mod.check_quota_account_identity("ok", core_env_prober=lambda: True)

    def test_probe_warns_instead_of_raising(self):
        # Content is irrelevant — the import fails before anything is parsed.
        self.plist.write_bytes(b"<?xml version='1.0'?><plist></plist>")
        with _BlockPlistlib():
            mod = _load_health_check("health_check_no_plistlib_3")
            out = self._run(mod)
        self.assertEqual(out["status"], "warn")
        self.assertIn("plistlib", out["detail"])
        self.assertIn("unaffected", out["detail"],
                      "the detail must say the other checks still ran, or a reader "
                      "will assume this is a whole-tool failure")

    def test_absent_plist_still_short_circuits_before_the_import(self):
        """A host with no credential proxy must be untouched by any of this —
        it returns ok before plistlib is ever needed."""
        self.assertFalse(self.plist.exists())
        with _BlockPlistlib():
            mod = _load_health_check("health_check_no_plistlib_4")
            out = self._run(mod)
        self.assertEqual(out["status"], "ok")
        self.assertIn("not launchd-managed", out["detail"])

    @unittest.skipIf(importlib.util.find_spec("plistlib") is None,
                     "interpreter cannot import plistlib")
    def test_no_regression_when_plistlib_is_available(self):
        """The lazy import must not change the working path: a real plist on a
        working interpreter still gets parsed, not warned about."""
        import plistlib
        self.plist.write_bytes(plistlib.dumps(
            {"Label": "com.sutando.credential-proxy",
             "EnvironmentVariables": {"HOME": str(self.home),
                                      "CLAUDE_CONFIG_DIR": "/tmp/x/.claude-sutando"}}))
        mod = _load_health_check("health_check_with_plistlib")
        with mock.patch.object(mod, "_keychain_service_exists", return_value=True):
            out = self._run(mod)
        self.assertNotIn("cannot import plistlib", out.get("detail", ""),
                         "a working interpreter must take the parsing path")


if __name__ == "__main__":
    unittest.main()
