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


if __name__ == "__main__":
    unittest.main()
