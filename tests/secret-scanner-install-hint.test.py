#!/usr/bin/env python3
"""secret_scanner.install_hint: the desktop's bundled python is told to update the app
(a pip install there is erased by the next engine update); a host python gets the pip
line with the PEP 668 fallback. vault_intercept's refusal carries the same hint.

Run: python3 tests/secret-scanner-install-hint.test.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from secret_scanner import install_hint, is_bundled_interpreter  # noqa: E402

BUNDLED = "/Users/o/Library/Application Support/space.ag2.app/engine/runtime/python/bin/python3"
HOST = "/opt/homebrew/bin/python3"


class InstallHint(unittest.TestCase):
    def test_bundled_python_updates_the_app_never_pips(self):
        self.assertTrue(is_bundled_interpreter(BUNDLED))
        h = install_hint(BUNDLED)
        self.assertIn("update the app", h)
        self.assertIn("fetch-scanner-deps.sh", h)
        self.assertIn("erased by the next engine update", h)
        self.assertNotIn("-m pip install", h)

    def test_host_python_gets_the_pip_line_for_that_interpreter(self):
        self.assertFalse(is_bundled_interpreter(HOST))
        h = install_hint(HOST)
        self.assertTrue(h.startswith(f"{HOST} -m pip install detect-secrets"))
        self.assertIn("--break-system-packages", h)

    def test_the_bash_twin_in_startup_sh_agrees(self):
        text = (Path(__file__).resolve().parent.parent / "src" / "startup.sh").read_text()
        self.assertIn("*/engine/runtime/python/*)", text)
        self.assertIn("update the app", text)
        self.assertIn("-m pip install detect-secrets", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
