#!/usr/bin/env python3
"""Execute Windows notification delegation with all external effects stubbed."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh") or shutil.which("powershell.exe")


@unittest.skipUnless(PWSH, "PowerShell is required")
class NotifyDelegation(unittest.TestCase):
    def test_python_delivery(self):
        self.check_delivery("python")

    def test_py_only_delivery(self):
        self.check_delivery("py")

    def check_delivery(self, interpreter):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            src = repo / "src"
            src.mkdir()
            (repo / "scripts").mkdir()
            shutil.copy(ROOT / "scripts/python-binary.ps1", repo / "scripts")
            shutil.copy(ROOT / "src/notify.ps1", src)
            (src / "workspace_default.ps1").write_text(
                "function Resolve-SutandoWorkspace { return $env:TEST_REPO }\n")
            (src / "dm-result.py").write_text(
                "import os, pathlib\ndef send_dm(text):\n"
                "    pathlib.Path(os.environ['TEST_REPO'], 'sent.txt').write_bytes(text.encode('utf-8'))\n"
                "    return False\n")
            wrapper = repo / "run.ps1"
            wrapper.write_text('''
function Get-Command {
    param($Name, $ErrorAction)
    if ($Name -eq $env:TEST_INTERPRETER) { return $Name }
}
function python { $input | & $env:TEST_PYTHON @args }
function py {
    if ($args[0] -ne '-3') { throw 'py requires -3' }
    $rest = $args[1..($args.Count - 1)]
    $input | & $env:TEST_PYTHON @rest
}
function Invoke-WebRequest { return @{ StatusCode = 426 } }
function Add-Type {
    Set-Content -Path (Join-Path $env:TEST_REPO 'desktop-reached') -Value 'yes'
    throw 'desktop disabled in test'
}
& (Join-Path $env:TEST_REPO 'src/notify.ps1') -Message $env:TEST_MESSAGE
''')
            message = '--help "quotes" $vars `literal` 雪\nsecond line\r\n'
            env = dict(os.environ, TEST_REPO=str(repo), TEST_PYTHON=sys.executable,
                       TEST_INTERPRETER=interpreter, TEST_MESSAGE=message, HOME=str(repo), USERPROFILE=str(repo))
            result = subprocess.run([PWSH, "-NoProfile", "-File", str(wrapper)],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / "sent.txt").exists(), "shared send_dm was not called")
            self.assertEqual((repo / "sent.txt").read_bytes(), message.encode("utf-8"))
            self.assertTrue((repo / "desktop-reached").exists())
            self.assertEqual(len(list((repo / "results").glob("proactive-*.txt"))), 1)


if __name__ == '__main__':
    unittest.main()
