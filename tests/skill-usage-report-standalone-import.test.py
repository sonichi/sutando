#!/usr/bin/env python3
"""report-usage.py imports on a standalone install: without core's `dotspace` module the Cloud
fallback is the manifest's declared default (or empty, and the report is skipped), the explicit
override still wins, and a missing manifest yields empty, never an exception at import.

Run: python3 tests/skill-usage-report-standalone-import.test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "skill-usage-report" / "scripts" / "report-usage.py"
MANIFEST = REPO / "skills" / "skill-usage-report" / "manifest.json"

# Loads the script with core's resolver unavailable, the way a standalone skill install sees it.
PROBE = r"""
import importlib.util, json, sys
sys.modules["dotspace"] = None
spec = importlib.util.spec_from_file_location("report_usage", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps({"fallback": mod.CLOUD_FALLBACK, "cloud": mod.CLOUD}))
"""


def _run(env_extra=None, script=SCRIPT):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AG2_CLOUD")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-I", "-c", PROBE, str(script)], capture_output=True,
                          text=True, env=env, timeout=60)


class StandaloneImport(unittest.TestCase):
    def test_imports_without_core_and_uses_the_manifest_default(self):
        r = _run()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        declared = json.loads(MANIFEST.read_text())["config"]["AG2_CLOUD_ORIGIN"]
        self.assertEqual(out["fallback"], declared)
        self.assertEqual(out["cloud"], declared)

    def test_explicit_override_wins_without_core(self):
        r = _run({"AG2_CLOUD_ORIGIN": "https://cloud.example"})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["cloud"], "https://cloud.example")

    def test_missing_manifest_yields_empty_not_an_exception(self):
        # The whole skill copied without its manifest: sibling modules import, the manifest misses.
        with tempfile.TemporaryDirectory() as d:
            skill = Path(d) / "skill"
            shutil.copytree(SCRIPT.parents[1], skill,
                            ignore=shutil.ignore_patterns("manifest.json", "__pycache__"))
            r = _run(script=skill / "scripts" / "report-usage.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["fallback"], "")


def _load_without_core(path=SCRIPT, env=None):
    """The script imported in THIS process with core's resolver unimportable, as a standalone
    install sees it; the caller restores sys.modules."""
    import importlib.util
    with mock.patch.dict(sys.modules, {"dotspace": None}), \
            mock.patch.dict(os.environ, env or {}, clear=False):
        for k in [k for k in os.environ if k.startswith("AG2_CLOUD") and k not in (env or {})]:
            del os.environ[k]
        spec = importlib.util.spec_from_file_location("report_usage_standalone", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class StandaloneImportInProcess(unittest.TestCase):
    """The same contract measured in-process: the manifest fallback branch and the skip."""

    def test_without_core_the_fallback_is_the_manifest_default(self):
        mod = _load_without_core()
        declared = json.loads(MANIFEST.read_text())["config"]["AG2_CLOUD_ORIGIN"]
        self.assertEqual((mod.CLOUD_FALLBACK, mod.CLOUD), (declared, declared))
        with mock.patch.dict(sys.modules, {"dotspace": None}):
            self.assertEqual(mod._cloud_fallback(), declared)

    def test_main_skips_cleanly_when_no_cloud_origin_is_known(self):
        import contextlib
        import io
        mod = _load_without_core()
        out = io.StringIO()
        with mock.patch.object(mod, "CLOUD", ""), contextlib.redirect_stdout(out):
            self.assertEqual(mod.main(), 0)
        self.assertIn("no cloud origin known", out.getvalue())


if __name__ == "__main__":
    unittest.main()
