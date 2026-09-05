#!/usr/bin/env python3
"""present-tour.py: text-only without a presenter; delegates with the right args when one exists."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "wizard" / "scripts" / "present-tour.py"


class PresentTourTests(unittest.TestCase):
    def test_no_presenter_is_text_only_and_exit_0(self):
        env = {**os.environ, "LOCAL_CARD_BIN": "/nonexistent/local-card.py"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--room", "!r:x"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("text-only", r.stdout)

    def test_presenter_gets_present_tour_with_room(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "local-card.py"
            log = Path(d) / "argv.json"
            stub.write_text(f"import json,sys; json.dump(sys.argv[1:], open({str(log)!r},'w')); print('lc_stub')\n")
            env = {**os.environ, "LOCAL_CARD_BIN": str(stub)}
            r = subprocess.run([sys.executable, str(SCRIPT), "--room", "!r:x"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("lc_stub", r.stdout)
            argv = json.loads(log.read_text())
            self.assertEqual(argv[:2], ["present", "tour"])
            self.assertIn("--room", argv)
            self.assertEqual(argv[argv.index("--room") + 1], "!r:x")

    def test_room_flag_without_value_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "local-card.py"
            stub.write_text("print('never')\n")
            env = {**os.environ, "LOCAL_CARD_BIN": str(stub)}
            r = subprocess.run([sys.executable, str(SCRIPT), "--room"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
