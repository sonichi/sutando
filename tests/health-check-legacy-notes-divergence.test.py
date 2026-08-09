#!/usr/bin/env python3
"""`legacy-notes-divergence` catches the pair the #1266 probe cannot reach.

`check_notes_split_brain` compares <repo>/notes to <workspace>/notes and returns
None when the repo has no notes/ dir — which is the normal post-migration state.
The pre-v0.8 <legacy workspace>/notes can still exist and still diverge, and
that split is invisible to an .md-only TOP-LEVEL glob because it lives in
`sutando-wire/episode-specs/*.yaml`. Both of those blind spots are asserted here.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


def _probe(ws_files, legacy_files, *, same_dir=False):
    """Build two notes trees and run the probe against them."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = root / "canonical"
        legacy_ws = root / "legacy"
        for base, files in ((ws / "notes", ws_files), (legacy_ws / "notes", legacy_files)):
            for rel in files:
                p = base / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
        target = ws / "notes" if same_dir else legacy_ws / "notes"
        with patch.object(hc, "WORKSPACE_DIR", ws), \
             patch.object(hc, "legacy_dotted_workspace", lambda: target.parent):
            return hc.check_legacy_notes_divergence()


print("the blind spot this exists for — a .yaml divergence nested three deep:")
r = _probe(["sutando-wire/episode-specs/a.yaml", "shared.md"],
           ["sutando-wire/episode-specs/b.yaml", "shared.md"])
check("fires on a nested non-.md divergence", r is not None and r["status"] == "warn", str(r))
check("reports BOTH directions", r is not None and "1 file(s) only in the canonical" in r["detail"]
      and "1 only in the legacy" in r["detail"], str(r))
check("names the shared count too", r is not None and "1 shared" in r["detail"], str(r))
check("claims no live-side verdict from a whole-tree mtime",
      r is not None and "newer by" not in r["detail"], str(r))

print("\nsilence where it should be silent:")
r = _probe(["a.md", "b.md"], ["a.md", "b.md"])
check("identical trees → no warn", r is None, str(r))

r = _probe(["a.md"], [])
check("legacy notes/ dir absent → no warn (nothing to diverge from)", r is None, str(r))

r = _probe(["a.md", "b.md"], ["a.md", "b.md"], same_dir=True)
check("same resolved dir → no warn (not two trees)", r is None, str(r))
# NOTE: that case passes even with the resolve() guard removed, because two views
# of one directory also produce an empty diff. The guard is a cheap short-circuit,
# not a behavioural gate, so it is deliberately NOT asserted as one — a mutation
# that deletes it does not change any output, and a test claiming otherwise would
# be asserting its own wishful thinking.

print("\nsupersets are still a divergence — neither side may be trusted blindly:")
r = _probe(["a.md"], ["a.md", "extra.yaml"])
check("legacy is a strict superset → still warns", r is not None, str(r))
check("...and says 0 only in the canonical",
      r is not None and "0 file(s) only in the canonical" in r["detail"], str(r))

print("\nthe #1266 probe is untouched:")
check("check_notes_split_brain still exists and is separate",
      callable(getattr(hc, "check_notes_split_brain", None)))
src = (REPO / "src" / "health-check.py").read_text()
# Match the CALL SITE, not the name: "check_legacy_notes_divergence()" also occurs
# in the `def` line, so the obvious assertion survives deleting the call entirely —
# measured, that mutation kept this file green.
check("the probe is CALLED, not merely defined",
      "_legacy_nd = check_legacy_notes_divergence()" in src
      and "checks.append(_legacy_nd)" in src)
check("the #1266 probe is still called too",
      "_notes_sb = check_notes_split_brain()" in src)

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — legacy-notes-divergence")
