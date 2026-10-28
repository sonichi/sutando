#!/usr/bin/env python3
"""The menu bar "Restart Sutando App" helper keeps the executable path as data.

restartSelf() runs /bin/sh -c with a constant script; the pid and the binary
path are positional parameters. A directory name holding shell syntax must be
exec'd as a path, never expanded. Runs the extracted script under /bin/sh.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "src" / "Sutando" / "main.swift"


def _restart_self_body() -> str:
    text = MAIN.read_text()
    start = text.index("@objc func restartSelf()")
    return text[start:text.index("\n    }\n", start)]


def _swift_literal(body: str) -> str:
    m = re.search(r'let script = "((?:[^"\\]|\\.)*)"', body)
    assert m, "restartSelf no longer defines `let script`"
    return re.sub(r'\\(.)', r'\1', m.group(1))


class RestartSelfRelaunchArgv(unittest.TestCase):
    def setUp(self):
        self.body = _restart_self_body()

    def test_path_with_shell_syntax_is_executed_literally(self):
        script = _swift_literal(self.body)
        dead = subprocess.Popen(["true"])
        dead.wait()
        with tempfile.TemporaryDirectory() as d:
            probe = os.path.join(d, "expanded")
            ran = os.path.join(d, "ran")
            odd = os.path.join(d, "$(touch expanded)`touch expanded`")
            os.mkdir(odd)
            binary = os.path.join(odd, "Sutando")
            Path(binary).write_text(f"#!/bin/sh\ntouch '{ran}'\n")
            os.chmod(binary, 0o755)
            subprocess.run(["/bin/sh", "-c", script, "sutando-relaunch", str(dead.pid), binary],
                           cwd=d, check=True, timeout=10)
            self.assertTrue(os.path.exists(ran), "the binary at the literal path did not run")
            self.assertFalse(os.path.exists(probe), "shell syntax in the path was expanded")


if __name__ == "__main__":
    unittest.main()
