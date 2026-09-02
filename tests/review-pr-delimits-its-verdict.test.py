#!/usr/bin/env python3
"""The verdict is everything after the LAST marker; the rest of stdout is codex's
trace, which --stall needs, so a consumer must extract instead of taking the stream."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-codex" / "scripts" / "review-pr.sh"
LEGACY_MARKER = "===CODEX-VERDICT==="
_ANNOUNCE = "VERDICT-" + "MARKER: "        # split so this file cannot self-match


def marker_of(stdout):
    """The token this RUN announced on line 1. A test cannot hardcode it — that is the
    point: a literal in a diff or verdict must not be able to pose as the marker."""
    first = stdout.splitlines()[0]
    assert first.startswith(_ANNOUNCE), f"line 1 is not the announcement: {first!r}"
    return first[len(_ANNOUNCE):].strip()

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
    def _run(self, diff="diff --git a/x b/x\n+real change\n", verdict=None):
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / "gh").write_text(f"#!/bin/bash\nprintf '%b' {diff!r}\n")
        # codex writes the clean verdict to -o and streams a trace to stdout
        (bin_dir / "codex").write_text(
            "#!/bin/bash\n"
            "out=''\n"
            'while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && { out="$2"; shift; }; shift; done\n'
            f"printf '%b' {TRACE!r}\n"
            f"[[ -n \"$out\" ]] && printf '%b' {(verdict or VERDICT)!r} > \"$out\"\n"
            "exit 0\n")
        for f in ("gh", "codex"):
            (bin_dir / f).chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        r = subprocess.run(["bash", str(SCRIPT), "1"], capture_output=True, text=True,
                           timeout=180, env=env)
        return r

    def test_the_marker_is_present_and_the_verdict_follows_it(self):
        r = self._run()
        self.assertIn(marker_of(r.stdout), r.stdout, f"no verdict marker in stdout (rc={r.returncode})")
        after = r.stdout.rsplit(marker_of(r.stdout), 1)[1].strip()
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
        after = r.stdout.rsplit(marker_of(r.stdout), 1)[1]
        self.assertNotIn("not_in_this_pr", after)
        self.assertNotIn("diff --git", after)

    def test_a_mechanical_failure_survives_marker_extraction(self):
        """A deterministic FAIL printed before the marker is dropped by the consumer."""
        bad = ('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n'
               '+TOKEN = "/Users/qingyun-air/.secret"\n')
        r = self._run(diff=bad)
        self.assertIn("review-checks", r.stdout, "the mechanical runner did not fire")
        after = r.stdout.rsplit(marker_of(r.stdout), 1)[1]
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
            self.assertIn(_ANNOUNCE.strip(), ln,
                          "the bridge must tell the agent to read the token from line 1")
            self.assertIn("LAST", ln, "must specify the LAST occurrence, not any")
            self.assertNotIn(LEGACY_MARKER, ln,
                             "naming a fixed literal is the defect: a diff can quote it")

    def test_the_marker_is_announced_on_line_one(self):
        tok = marker_of(self._run().stdout)
        self.assertTrue(tok.startswith("===CODEX-VERDICT-"), tok)
        self.assertNotEqual(tok, LEGACY_MARKER, "a fixed literal is the defect")

    def test_the_nonce_differs_between_runs(self):
        a, b = marker_of(self._run().stdout), marker_of(self._run().stdout)
        self.assertNotEqual(a, b, "a per-run nonce that repeats IS a fixed literal")

    def test_a_verdict_quoting_the_LEGACY_marker_cannot_truncate_the_extract(self):
        """A verdict may legitimately quote the legacy literal, so the split point
        must be the per-run nonce; matching the literal would truncate the extract."""
        poisoned = (f"the `{LEGACY_MARKER}` marker is sound.\n"
                    "- src/foo.py:12 real bug: off-by-one\n")
        r = self._run(verdict=poisoned)
        tok = marker_of(r.stdout)
        extract = r.stdout.rsplit(tok, 1)[1]
        self.assertIn("off-by-one", extract, "the verdict body was truncated")
        self.assertIn("Mechanical checks", extract,
                      "a review-checks FAIL would be silently dropped")

    def test_a_diff_quoting_the_LEGACY_marker_does_not_leak_the_nonce(self):
        """Codex is never shown the nonce, so it cannot echo it however the diff reads."""
        r = self._run(diff=f"diff --git a/x b/x\n+{LEGACY_MARKER}\n")
        tok = marker_of(r.stdout)
        after_announce = r.stdout.split("\n", 1)[1]
        self.assertEqual(after_announce.count(tok), 1,
                         "the nonce must appear exactly once after line 1")

    def test_the_skill_stdout_contract_names_the_marker(self):
        """SKILL.md documents the same path the in-band block runs; both or neither."""
        text = (REPO / "skills" / "claude-codex" / "SKILL.md").read_text()
        # Anchor on the CONTRACT, not on prose: the sentence around it gets reworded.
        idx = text.find(_ANNOUNCE.strip())
        self.assertNotEqual(idx, -1, "the stdout contract paragraph is gone")
        para = text[max(0, idx - 100):idx + 700]
        self.assertIn(_ANNOUNCE.strip(), para,
                      "SKILL.md must document the line-1 announcement")
        self.assertIn("LAST", para, "must specify the LAST occurrence, not any")


if __name__ == "__main__":
    unittest.main()
