#!/usr/bin/env python3
"""The sparkline JS, executed: a diagonal-crossing stretch splits at y=x.

Reviewer control (#3464, 4th round): the preserved flat run (.1,.25)->(.8,.25)
crosses even pace at (.25,.25); midpoint coloring painted it wholly green and
the owner-visible over-pace meaning vanished. This pins the EXECUTED output —
the JS runs under node with fetch/document stubbed — not the backend points.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

NODE = shutil.which("node")

PAYLOAD = {
    "now": 0,
    "windows": {
        "5h": {"span_s": 18000, "segments": [{
            "reset": 1, "current": True,
            "points": [{"x": 0.1, "y": 0.25}, {"x": 0.8, "y": 0.25}],
            "projected_end": 0.3125,
        }]},
        "7d": {"span_s": 604800, "segments": []},
    },
}

HARNESS = """
const svgs = {};
global.document = {getElementById: id => svgs[id] || (svgs[id] = {innerHTML: ""})};
global.fetch = async () => ({json: async () => (%PAYLOAD%)});
%SCRIPT%
setTimeout(() => console.log(JSON.stringify(svgs)), 200);
"""


class SparkColors(unittest.TestCase):
    @unittest.skipUnless(NODE, "node not available")
    def test_a_diagonal_crossing_stretch_shows_both_colors(self):
        import dashboard
        js = re.sub(r"</?script>", "", dashboard._QUOTA_SPARK_JS)
        js = js.split("<div", 1)[0] if js.lstrip().startswith("<div") else js
        harness = HARNESS.replace("%PAYLOAD%", json.dumps(PAYLOAD)).replace("%SCRIPT%", js)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            path = f.name
        r = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        svgs = json.loads(r.stdout.strip().splitlines()[-1])
        html = svgs.get("qs-5h", {}).get("innerHTML", "")
        # measured strokes only: width 1.5 lines (diagonal + projection are dashed)
        measured = re.findall(r'<line [^>]*stroke="(#[0-9a-f]{6})" stroke-width="1.5"', html)
        self.assertIn("#e94560", measured, "over-pace portion must draw red")
        self.assertIn("#4ecca3", measured, "under-pace portion must draw green")
        # the split point sits at x=y=.25: a red line must END where a green one STARTS
        red_then_green = re.search(
            r'x2="([\d.]+)" y2="([\d.]+)" stroke="#e94560"[^>]*/>'
            r'<line x1="\1" y1="\2"[^>]*stroke="#4ecca3"', html)
        self.assertTrue(red_then_green, "stretch must split at the diagonal crossing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
