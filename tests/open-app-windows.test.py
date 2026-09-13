"""Execute the Windows launcher policy and native binding contracts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell app launcher")
class OpenAppWindowsTests(unittest.TestCase):
    def run_script(self, script, *args):
        powershell = shutil.which("pwsh")
        self.assertIsNotNone(powershell, "PowerShell 7 is required")
        return subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
             "-File", str(script), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )

    def test_production_policy_with_controlled_windows_and_actions(self):
        result = self.run_script(ROOT / "tests" / "open-app-windows.test.ps1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: app identification and foreground verification", result.stdout)

    def test_cli_delegates_to_loadable_native_backend(self):
        result = self.run_script(ROOT / "scripts" / "open-app.ps1", "-ValidateOnly")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": "ok", "platform": "windows", "api": "win32",
        })

    def test_backend_runs_without_repo_files_from_a_spaced_bundle_path(self):
        with tempfile.TemporaryDirectory(prefix="app bundle ") as folder:
            backend = Path(folder) / "windows-app-launcher.ps1"
            shutil.copyfile(ROOT / "src" / backend.name, backend)
            result = self.run_script(backend, "-ValidateOnly")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
