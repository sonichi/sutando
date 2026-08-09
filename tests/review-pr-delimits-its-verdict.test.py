#!/usr/bin/env python3
"""The verdict is everything after the LAST marker; the rest of stdout is codex's
trace, which --stall needs, so a consumer must extract instead of taking the stream.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-codex" / "scripts" / "review-pr.sh"
MARKER = "===CODEX-VERDICT==="

# A trace that looks like a diff, which is the whole trap.
TRACE = """thinking about the diff
+def test_something_that_is_not_in_this_pr(self):
+    assert supersede_on_inflight()
diff --git a/src/other.py b/src/other.py
tokens used
41,926
"""
VERDICT = "no blocking issues\nbut check the deferral path\n"


class ReviewPrDelimitsItsVerdict(unittest.TestCase):
    def _run(self, diff="diff --git a/x b/x\n+real change\n"):
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / "gh").write_text(f"#!/bin/bash\nprintf '%b' {diff!r}\n")
        # codex writes the clean verdict to -o and streams a trace to stdout
        (bin_dir / "codex").write_text(
            "#!/bin/bash\n"
            "out=''\n"
            'while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && { out="$2"; shift; }; shift; done\n'
            f"printf '%b' {TRACE!r}\n"
            f"[[ -n \"$out\" ]] && printf '%b' {VERDICT!r} > \"$out\"\n"
            "exit 0\n")
        for f in ("gh", "codex"):
            (bin_dir / f).chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        r = subprocess.run(["bash", str(SCRIPT), "1"], capture_output=True, text=True,
                           timeout=180, env=env)
        return r

    def test_the_marker_is_present_and_the_verdict_follows_it(self):
        r = self._run()
        self.assertIn(MARKER, r.stdout, f"no verdict marker in stdout (rc={r.returncode})")
        after = r.stdout.rsplit(MARKER, 1)[1].strip()
        # The extracted region is the mechanical findings THEN the codex verdict:
        # both are review output, and only the trace belongs on the other side.
        self.assertTrue(after.endswith(VERDICT.strip()), after)
        self.assertIn("Mechanical checks", after)
        self.assertLess(after.index("Mechanical checks"), after.index("no blocking issues"))
        self.assertNotIn("thinking about the diff", after, "no trace may cross the marker")

    def test_the_trace_is_still_on_stdout_so_the_stall_watchdog_keeps_working(self):
        """Silencing codex would fix the parsing and break the watchdog. It must stay."""
        r = self._run()
        self.assertIn("thinking about the diff", r.stdout,
                      "the agent trace must remain on stdout — --stall watches it")

    def test_a_consumer_splitting_on_the_marker_cannot_quote_the_trace(self):
        """The actual defect: diff-shaped lines in the trace must not reach the verdict."""
        r = self._run()
        after = r.stdout.rsplit(MARKER, 1)[1]
        self.assertNotIn("not_in_this_pr", after)
        self.assertNotIn("diff --git", after)

    def test_a_mechanical_failure_survives_marker_extraction(self):
        """A deterministic FAIL printed before the marker is dropped by the consumer."""
        bad = ('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n'
               '+TOKEN = "/Users/qingyun-air/.secret"\n')
        r = self._run(diff=bad)
        self.assertIn("review-checks", r.stdout, "the mechanical runner did not fire")
        after = r.stdout.rsplit(MARKER, 1)[1]
        # codex says "no blocking issues" here, so the extracted text is the ONLY
        # place a reader would see the hardcoded path.
        self.assertIn("hardcoded path", after,
                      "a mechanical FAIL must survive extraction, not just appear on stdout")
        self.assertIn("no blocking issues", after, "the codex verdict must still be there")

    def test_the_bridge_instruction_tells_the_agent_to_extract_after_the_marker(self):
        """The only production consumer: a bare 'verdict on stdout' reproduces the defect."""
        text = (REPO / "src" / "discord-bridge.py").read_text()
        success = [ln for ln in text.splitlines()
                   if "On SUCCESS" in ln and "results/task-{id}.txt" in ln]
        self.assertTrue(success, "PR-REVIEW success instruction not found in the bridge")
        for ln in success:
            self.assertIn(MARKER, ln,
                          "the bridge must name the marker, not just 'verdict on stdout'")
            self.assertIn("LAST", ln, "must specify the LAST marker, not any marker")

    def test_the_skill_stdout_contract_names_the_marker(self):
        """SKILL.md documents the same path the in-band block runs; both or neither."""
        text = (REPO / "skills" / "claude-codex" / "SKILL.md").read_text()
        idx = text.find("Prints Codex's verdict to stdout")
        self.assertNotEqual(idx, -1, "the stdout contract paragraph is gone")
        para = text[idx:idx + 700]
        self.assertIn(MARKER, para,
                      "SKILL.md must name the marker where it describes stdout")
        self.assertIn("LAST", para, "must specify the LAST marker, not any marker")


if __name__ == "__main__":
    unittest.main()
