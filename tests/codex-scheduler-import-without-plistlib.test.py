#!/usr/bin/env python3
"""`install` needs plistlib; `tick` and `health` must not pay for it.

`plistlib` was imported at MODULE scope but is used at exactly one site — inside
`install()`, to write the launchd plist. It pulls in `xml.parsers.expat` -> the
`pyexpat` C extension, which dlopens libexpat, so a Python whose pyexpat was
built against a different libexpat than it finds at runtime raises ImportError
at import time.

At module scope that killed EVERY subcommand, including:
  * `tick`   — the job launchd invokes once per minute (the whole durable path),
  * `health` — whose entire job is to report that the scheduler is broken.

Neither writes a plist. Measured on a live host 2026-08-03, same file and commit:

    /opt/homebrew/bin/python3 3.14.5 -> tick and health both die with
                                        ImportError: dlopen … pyexpat …
                                        _XML_SetAllocTrackerActivationThreshold
    /usr/bin/python3          3.9.6  -> both reach real logic

A durable scheduler that cannot run is worse than one that is absent: the
launchd job stays loaded and `--status` still reports it. Sibling of the same
defect shape in `src/health-check.py` (#2588).

`test_module_imports_when_plistlib_is_unavailable` is the load-bearing case — it
FAILS on the parent commit. The blocker-control cases exist because a blocker
that silently no-opped would make every other case here pass for the wrong
reason.

Hermetic: no launchctl, no plist written outside a temp HOME, no ffprobe.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "schedule-crons" / "scripts" / "codex-scheduler.py"


class _BlockPlistlib:
    """Make `import plistlib` raise ImportError, as a broken pyexpat does.

    Simulated, not reproduced: reproducing it requires a mismatched libexpat,
    which is a property of the host. The symptom is what the code must survive.
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


def _load(module_name):
    """Fresh module name per load — a cached sys.modules entry would let the
    module appear to import under the blocker when it had really been imported
    earlier, unblocked, so the load-bearing case could not fail even at parent."""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return mod


class BlockerControl(unittest.TestCase):
    def test_the_blocker_actually_blocks(self):
        """If this cannot go positive, nothing below is evidence."""
        with _BlockPlistlib():
            with self.assertRaises(ImportError):
                import plistlib  # noqa: F401

    def test_the_blocker_leaves_the_rest_of_the_stdlib_alone(self):
        with _BlockPlistlib():
            import json  # noqa: F401
            import subprocess  # noqa: F401

    def test_plistlib_is_restored_afterwards(self):
        with _BlockPlistlib():
            pass
        import plistlib
        self.assertTrue(hasattr(plistlib, "dumps"))


class ImportWithoutPlistlib(unittest.TestCase):
    def test_module_imports_when_plistlib_is_unavailable(self):
        """THE pin. On the parent commit this raises at module scope and every
        subcommand — tick, health, install — is lost with it."""
        with _BlockPlistlib():
            mod = _load("codex_scheduler_no_plistlib")
        self.assertTrue(hasattr(mod, "install"))

    def test_the_runner_entrypoints_survive(self):
        """Importing only matters if the durable path came with it."""
        with _BlockPlistlib():
            mod = _load("codex_scheduler_no_plistlib_2")
        for fn in ("install", "main"):
            self.assertTrue(callable(getattr(mod, fn, None)), f"{fn} missing")

    def test_module_does_not_import_plistlib_at_module_scope(self):
        """Structural guard against a future edit hoisting it back to the top —
        the regression would be silent on any host whose pyexpat works, which is
        every CI runner."""
        src = SCRIPT.read_text()
        module_scope = [
            ln for ln in src.splitlines()
            if ln.startswith("import plistlib") or ln.startswith("from plistlib")
        ]
        self.assertEqual(
            module_scope, [],
            "plistlib must be imported inside install(), not at module scope — "
            "it is used at exactly one site and a top-level import takes down "
            "tick (launchd, every minute) and health with it",
        )


class InstallFailsCleanly(unittest.TestCase):
    def test_install_raises_SystemExit_not_ImportError(self):
        """install genuinely cannot proceed without plistlib — but it must say so
        in one actionable line, not a dlopen traceback the operator has to decode."""
        with _BlockPlistlib():
            mod = _load("codex_scheduler_no_plistlib_3")
            src = SCRIPT.read_text()
            self.assertIn("cannot import plistlib", src,
                          "the install path must name the cause")
            self.assertIn("SystemExit", src,
                          "install must exit cleanly rather than raise ImportError")
        self.assertTrue(hasattr(mod, "install"))


if __name__ == "__main__":
    unittest.main()
