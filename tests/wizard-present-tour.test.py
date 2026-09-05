#!/usr/bin/env python3
"""present-tour.py: text-only without a presenter; finds the desktop presenter by the shipped layout
with no override; delegates with the right args. Run under the floor interpreter too (3.9)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "wizard" / "scripts" / "present-tour.py"


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)


def stub_presenter(path: Path, log: Path):
    path.write_text(f"import json,sys; json.dump(sys.argv[1:], open({str(log)!r},'w')); print('lc_stub')\n")


class PresentTourTests(unittest.TestCase):
    def test_no_presenter_is_text_only_and_exit_0(self):
        r = run(SCRIPT, "--room", "!r:x", "--presenter", "/nonexistent/local-card.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("text-only", r.stdout)

    def test_shipped_layout_is_found_without_any_override(self):
        # engine/local-card.py beside engine/sutando/skills/wizard/scripts/present-tour.py
        with tempfile.TemporaryDirectory() as d:
            engine = Path(d) / "engine"
            script = engine / "sutando" / "skills" / "wizard" / "scripts" / "present-tour.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(SCRIPT, script)
            log = Path(d) / "argv.json"
            stub_presenter(engine / "local-card.py", log)
            r = run(script, "--room", "!r:x")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("lc_stub", r.stdout)
            argv = json.loads(log.read_text())
            self.assertEqual(argv[:2], ["present", "tour"])
            self.assertEqual(argv[argv.index("--room") + 1], "!r:x")

    def test_explicit_presenter_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "local-card.py"
            log = Path(d) / "argv.json"
            stub_presenter(stub, log)
            r = run(SCRIPT, "--presenter", str(stub))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("--room", json.loads(log.read_text()))

    def test_room_flag_without_value_is_refused(self):
        r = run(SCRIPT, "--room")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
