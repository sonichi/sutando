#!/usr/bin/env python3
"""CI guard: the ag2-sparrow package's bundled modules must stay in sync with
their canonical src/ source (option A). Which modules those are is MAP in
sync_from_src.py, never a list restated here; modules absent from MAP are
package-canonical and intentionally diverge.

This lives under tests/ so the repo's test discovery
(`find tests -name '*.test.py'`, package.json) actually runs it — the package's
own `tools/test_no_drift.py` was outside every discovery path (Codex #2082 P2).

Run: python3 tests/ag2-sparrow-drift.test.py
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYNC = REPO / "packages" / "ag2-sparrow" / "tools" / "sync_from_src.py"


def test_ag2_sparrow_bundled_utils_in_sync_with_src():
    assert SYNC.exists(), f"missing {SYNC}"
    r = subprocess.run([sys.executable, str(SYNC), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "ag2-sparrow bundled utils drifted from src/. "
        "Run: python3 packages/ag2-sparrow/tools/sync_from_src.py\n"
        + (r.stdout or "") + (r.stderr or "")
    )


if __name__ == "__main__":
    test_ag2_sparrow_bundled_utils_in_sync_with_src()
    print("PASS — ag2-sparrow bundled utils in sync with src/")
