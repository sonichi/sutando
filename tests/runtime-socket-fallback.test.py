#!/usr/bin/env python3
"""The runtime-socket helper must not hard-fail when the delegate is unavailable.

`sutando-config.sh` sets `set -euo pipefail`, so a failing command substitution
terminates the script at the assignment and the fallback below it never runs —
the branch whose comment says the helper must never hard-fail.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNDIR = REPO / "src" / "runtime-api" / "rundir.py"


def runtime_socket(env=None):
    p = subprocess.run(["bash", "scripts/sutando-config.sh", "runtime-socket"],
                       cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    return p.returncode, p.stdout.strip()


class FallbackIsReachable(unittest.TestCase):
    def test_it_answers_when_the_delegate_is_present(self):
        rc, out = runtime_socket()
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("sutando-runtime.sock"), out)

    def test_it_still_answers_when_the_delegate_is_missing(self):
        # The regression: errexit killed the script at the assignment, so this
        # returned rc=1 and zero bytes to every caller that shells out to it.
        if not RUNDIR.exists():
            self.skipTest("rundir.py absent in this checkout")
        with tempfile.TemporaryDirectory() as td:
            stash = pathlib.Path(td) / "rundir.py"
            shutil.move(str(RUNDIR), str(stash))
            try:
                rc, out = runtime_socket()
            finally:
                shutil.move(str(stash), str(RUNDIR))
        self.assertEqual(rc, 0, "the helper hard-failed; callers get an empty path")
        self.assertTrue(out.endswith("sutando-runtime.sock"), out or "<empty>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
