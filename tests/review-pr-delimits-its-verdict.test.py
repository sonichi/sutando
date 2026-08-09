#!/usr/bin/env python3
"""review-pr.sh must delimit its verdict, so a consumer never quotes the trace.

codex's own exec trace goes to review-pr.sh's stdout UNREDIRECTED, and that is
deliberate: codex-bounded.sh --stall watches that stream to tell a working run
from a wedged one, so silencing it would break the watchdog. But the trace
contains source the agent inlined while working, so a consumer that reads "the
tail" can quote repository code as the PR's own content.

That is not hypothetical. Reviewing sonichi/sutando#2763 I read test names out of
the tail of a 64KB dump and reported them as the PR's coverage; `grep` over the
actual diff showed all four were absent. Everything after the LAST marker is the
verdict, and nothing else is.

Stubs `gh` and `codex` on PATH — no network, no agent, no cost.
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
    def _run(self):
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / "gh").write_text("#!/bin/bash\necho 'diff --git a/x b/x'\necho '+real change'\n")
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
        self.assertEqual(after, VERDICT.strip(),
                         "splitting on the last marker must yield exactly the verdict")

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


if __name__ == "__main__":
    unittest.main()
