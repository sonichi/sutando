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
    """Build two notes trees and run the probe against them.

    A list entry may be "path" (contents "x") or a ("path", "contents") pair, so a
    fixture can put the SAME path on both sides with DIFFERENT bytes.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = root / "canonical"
        legacy_ws = root / "legacy"
        for base, files in ((ws / "notes", ws_files), (legacy_ws / "notes", legacy_files)):
            for entry in files:
                rel, body = entry if isinstance(entry, tuple) else (entry, "x")
                p = base / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
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

# A name-set comparison reports healthy when both trees hold one path with
# different bytes, so deleting either side loses bytes it called fine.
print("\nsame path, DIFFERENT bytes (the P1):")
r = _probe([("sutando-wire/episode-specs/a.yaml", "a: 1\n")],
           [("sutando-wire/episode-specs/a.yaml", "a: 2\n")])
check("identical NAMES but differing content still warns", r is not None, str(r))
check("...and says how many differ",
      r is not None and "1 shared path(s) differ in CONTENT" in r["detail"], str(r))
check("...and names one of them",
      r is not None and "episode-specs/a.yaml" in r["detail"], str(r))
check("...and reports 0 in each only-side (the sets really are equal)",
      r is not None and "0 file(s) only in the canonical" in r["detail"]
      and "0 only in the legacy" in r["detail"], str(r))
check("...and does NOT claim a superset, since names match on both sides",
      r is not None and "strict superset" not in r["detail"]
      and "Neither side is a superset" not in r["detail"], str(r))

# An unreadable file must never compare EQUAL to another — that would convert a
# read failure into a silent "identical".
check("the digest sentinel cannot collide with a real digest",
      hc._file_digest(Path("/nonexistent/x")) != hc._file_digest(Path("/nonexistent/y")))

print("\nsilence where it should be silent:")
r = _probe(["a.md", "b.md"], ["a.md", "b.md"])
check("identical trees → no warn", r is None, str(r))

r = _probe(["a.md"], [])
check("legacy notes/ dir absent → no warn (nothing to diverge from)", r is None, str(r))

r = _probe(["a.md", "b.md"], ["a.md", "b.md"], same_dir=True)
check("same resolved dir → no warn (not two trees)", r is None, str(r))
# Deleting the resolve() guard changes no output here: two views of one dir also
# give an empty diff. It is a short-circuit, so it is NOT asserted as a gate.

# A bare ratio invites dismissal, so the legacy-only paths must be NAMED — they
# are the ones a "delete the legacy tree" cleanup destroys.
print("\nthe at-risk paths are named, not just counted:")
r = _probe(["keep.md"], ["keep.md", "sutando-wire/peg-signals/2026-08-05.md"])
check("names the legacy-only path",
      r is not None and "sutando-wire/peg-signals/2026-08-05.md" in r["detail"], str(r))
check("labels it as having no canonical copy",
      r is not None and "LEGACY-ONLY" in r["detail"], str(r))

r = _probe(["keep.md"], ["keep.md"] + [f"only{i}.md" for i in range(7)])
check("caps the sample and says how many more",
      r is not None and "and 4 more" in r["detail"], str(r))
check("...without dropping the true total",
      r is not None and "7 only in the legacy tree" in r["detail"], str(r))

# A plain alphabetical sample leads with .DS_Store and buries what matters —
# measured on the live tree, where it crowded out sutando-wire/peg-signals/*.md.
r = _probe(["keep.md"], ["keep.md", ".DS_Store", ".aaa", ".bbb",
                         "sutando-wire/peg-signals/x.md"])
check("dotfiles do not crowd the 3-wide sample",
      r is not None and "sutando-wire/peg-signals/x.md" in r["detail"], str(r))
check("...and the count still includes the dotfiles",
      r is not None and "4 only in the legacy tree" in r["detail"], str(r))

r = _probe(["canonical-only.md", "keep.md"], ["keep.md"])
check("no legacy-only paths → no LEGACY-ONLY clause at all",
      r is not None and "LEGACY-ONLY" not in r["detail"], str(r))

print("\nsupersets are still a divergence — neither side may be trusted blindly:")
r = _probe(["a.md"], ["a.md", "extra.yaml"])
check("legacy is a strict superset → still warns", r is not None, str(r))
check("...and says 0 only in the canonical",
      r is not None and "0 file(s) only in the canonical" in r["detail"], str(r))
# The shape sentence must be DERIVED: hardcoding "neither side is a superset"
# states the opposite of the sets whenever one side actually is.
check("...and does NOT claim 'neither side is a superset'",
      r is not None and "Neither side is a superset" not in r["detail"], str(r))
check("...it names WHICH side is the superset",
      r is not None and "legacy tree is a strict superset" in r["detail"], str(r))

r = _probe(["a.md", "onlyhere.md"], ["a.md"])
check("canonical is the superset → the sentence flips",
      r is not None and "canonical workspace is a strict superset" in r["detail"], str(r))

r = _probe(["a.md", "x.md"], ["a.md", "y.md"])
check("both sides exclusive → 'neither side is a superset' is CORRECT here",
      r is not None and "Neither side is a superset" in r["detail"], str(r))

print("\nthe #1266 probe is untouched:")
check("check_notes_split_brain still exists and is separate",
      callable(getattr(hc, "check_notes_split_brain", None)))
src = (REPO / "src" / "health-check.py").read_text()
# Match the CALL SITE: the bare name also occurs in the `def` line, so asserting
# the name alone survives deleting the call.
check("the probe is CALLED, not merely defined",
      "_legacy_nd = check_legacy_notes_divergence()" in src
      and "checks.append(_legacy_nd)" in src)
check("the #1266 probe is still called too",
      "_notes_sb = check_notes_split_brain()" in src)

# The two `except OSError` arms decide what happens when the filesystem will not
# answer, and an untested fail-safe is indistinguishable from one that never runs.
print("\nfail-safe branches, driven rather than asserted:")
with tempfile.TemporaryDirectory() as _td:
    _root = Path(_td)
    _ws, _legacy = _root / "canonical", _root / "legacy"
    for _base in (_ws / "notes", _legacy / "notes"):
        _base.mkdir(parents=True)
        (_base / "a.md").write_text("x")

    _real_resolve = Path.resolve

    def _boom_resolve(self, *a, **k):
        if str(self).startswith(str(_legacy)):
            raise OSError("simulated resolve failure")
        return _real_resolve(self, *a, **k)

    with patch.object(hc, "WORKSPACE_DIR", _ws), \
         patch.object(hc, "legacy_dotted_workspace", lambda: _legacy), \
         patch.object(Path, "resolve", _boom_resolve):
        r = hc.check_legacy_notes_divergence()
    check("resolve() raising OSError WARNS — a read failure is not 'no divergence'",
          r is not None and r.get("status") == "warn", str(r))
    check("...and says it could not compare, not that the trees agree",
          r is not None and "could not compare" in r["detail"], str(r))
    check("...and refuses to read as a clean bill of health",
          r is not None and "NOT a clean bill of health" in r["detail"], str(r))

    _real_rglob = Path.rglob

    def _boom_rglob(self, pattern):
        if str(self).startswith(str(_legacy)):
            raise OSError("simulated listing failure")
        return _real_rglob(self, pattern)

    with patch.object(hc, "WORKSPACE_DIR", _ws), \
         patch.object(hc, "legacy_dotted_workspace", lambda: _legacy), \
         patch.object(Path, "rglob", _boom_rglob):
        r = hc.check_legacy_notes_divergence()
    check("an unreadable tree WARNS rather than reporting a partial comparison",
          r is not None and r.get("status") == "warn", str(r))
    check("...and names enumeration as what failed, not a file count",
          r is not None and "enumerating" in r["detail"], str(r))

# The case a non-empty canonical side hides: with nothing on the readable side, a
# partial scan compares empty-to-empty and the only copy of every file looks fine.
with tempfile.TemporaryDirectory() as _td2:
    _root2 = Path(_td2)
    _ws2, _legacy2 = _root2 / "canonical", _root2 / "legacy"
    (_ws2 / "notes").mkdir(parents=True)
    (_legacy2 / "notes").mkdir(parents=True)
    (_legacy2 / "notes" / "only-copy.md").write_text("the only copy of this file")

    _rg2 = Path.rglob

    def _boom_rglob2(self, pattern):
        if str(self).startswith(str(_legacy2)):
            raise OSError("unreadable")
        return _rg2(self, pattern)

    with patch.object(hc, "WORKSPACE_DIR", _ws2), \
         patch.object(hc, "legacy_dotted_workspace", lambda: _legacy2), \
         patch.object(Path, "rglob", _boom_rglob2):
        r = hc.check_legacy_notes_divergence()
    check("unreadable legacy + EMPTY canonical still WARNS (the only-copy case)",
          r is not None and r.get("status") == "warn", str(r))

# The source-substring check above survives deleting the append; only calling the
# aggregate proves the result actually reaches an operator.
_synthetic = {"name": "legacy-notes-divergence", "status": "warn", "detail": "synthetic"}
with patch.object(hc, "check_legacy_notes_divergence", lambda: _synthetic):
    _names = [c.get("name") for c in hc.run_all_checks()]
check("run_all_checks() APPENDS the result — executed, not grepped",
      "legacy-notes-divergence" in _names)

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — legacy-notes-divergence")
