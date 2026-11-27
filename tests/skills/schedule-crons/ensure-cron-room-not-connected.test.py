#!/usr/bin/env python3
"""ensure-cron-room with no gateway credential anywhere: it says so in the product's name,
creates nothing and exits 0, so a cron run on an unconnected install is not an error."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402
from _helpers.subprocess_coverage import covered_code  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "schedule-crons" / "ensure-cron-room.py"


def run_script(*args: str) -> str:
    """Code that runs the script as `__main__` with `args`, so a child under the suite's coverage is measured."""
    return covered_code("import runpy, sys; sys.argv = %r; runpy.run_path(%r, run_name='__main__')"
                        % ([str(SCRIPT), *args], str(SCRIPT)))


class NotConnected(unittest.TestCase):
    def test_no_token_anywhere_skips_and_names_the_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            # An empty --repo: no src/connections.py to route through and no .env to read.
            crons = Path(tmp) / "crons.json"
            crons.write_text('[{"name": "daily", "room": "auto"}]', encoding="utf-8")
            env = hermetic_env(home=tmp, CLAUDE_CONFIG_DIR=os.path.join(tmp, "ccd"))
            r = subprocess.run([sys.executable, "-c", run_script("--crons-file", str(crons), "--repo", tmp)],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ensure-cron-room: not connected to s.space (no gateway token)", r.stdout)
        self.assertNotIn("AG2 Space", r.stdout)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
