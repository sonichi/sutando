#!/usr/bin/env python3
"""A `|| fallback` after a pipeline never runs — the last stage decides the status.

`cmd | grep … | head -40 || echo "_(no diff)_"` binds `||` to `head`, which exits 0
on empty input however `grep` fared. The fallback is unreachable, so "nothing
found" renders as an empty section — indistinguishable from "the command did not
run". Same defect class as #2379/#2381/#2383. THREE production sites, not two:
my own regression guard below found the third after I thought I was done.

Shellcheck does not catch it (`cmd | cmd || fallback` is valid shell), and
`scripts/sutando-migrate.sh` has been under a shellcheck gate since #2188 — so a
behavioural test is the only thing that holds the line.

Both directions are asserted. Without the CONTROL that real output still flows,
the fix could pass by always printing the fallback.

Run: python3 tests/unreachable-pipeline-fallbacks.test.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def sh(script: str) -> str:
    """Run a bash fragment, return stdout+stderr."""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return (r.stdout or "") + (r.stderr or "")


# --- the shape itself, so the test documents WHY -----------------------------
broken = sh('echo x | grep -E "^Only in " | head -40 || echo "_(no diff)_"')
ok("BASELINE: the retired shape never reaches its fallback",
   "_(no diff)_" not in broken, f"got {broken!r}")
control = sh('echo x | grep -E "^Only in " || echo "_(no diff)_"')
ok("BASELINE control: without the trailing pipe stage, it DOES fire",
   "_(no diff)_" in control, f"got {control!r}")


# --- site 1: gather-remote.sh ------------------------------------------------
g = (REPO / "skills/self-diagnose/scripts/gather-remote.sh").read_text()
frag = re.search(r"_only=\$\(diff.*?unset _only", g, re.S)
ok("gather-remote: the diff section captures before testing", bool(frag))
if frag:
    body = frag.group(0)
    empty = sh(f'''
      OUT=$(mktemp -d); mkdir -p "$OUT/local" "$OUT/remote"   # identical trees
      {body}
    ''')
    ok("gather-remote: identical trees now SAY '_(no diff)_'",
       "_(no diff)_" in empty, f"got {empty!r}")
    # CONTROL — a real difference must still be listed, not replaced by the fallback.
    diff = sh(f'''
      OUT=$(mktemp -d); mkdir -p "$OUT/local" "$OUT/remote"; : > "$OUT/local/only-here.txt"
      {body}
    ''')
    ok("CONTROL: gather-remote still lists a real difference",
       "only-here.txt" in diff and "_(no diff)_" not in diff, f"got {diff!r}")


# --- site 3: gather-remote.sh DRIFT block -----------------------------------
# Found by this file's own regression guard AFTER I had "finished" the fix — it
# was in my first scan and fell out when I narrowed the list. The guard catching
# a present-day omission, not a future one, is the reason it asserts on the file
# rather than only on behaviour.
frag3 = re.search(r"_drift=\$\(git -C.*?unset _drift", g, re.S)
ok("gather-remote: the DRIFT block captures before testing", bool(frag3))
if frag3:
    body3 = frag3.group(0)
    bad_range = sh(f'''
      LOCAL_REPO=$(mktemp -d); git -C "$LOCAL_REPO" init -q 2>/dev/null
      REMOTE_HEAD=deadbeef; LOCAL_HEAD=HEAD      # a range git cannot resolve
      {body3}
    ''')
    ok("gather-remote: an unresolvable range now SAYS 'could not compute'",
       "could not compute" in bad_range, f"got {bad_range!r}")


# --- site 2: sutando-migrate.sh ----------------------------------------------
m = (REPO / "scripts/sutando-migrate.sh").read_text()
frag2 = re.search(r"_backups=\$\(ls -1.*?unset _backups", m, re.S)
ok("sutando-migrate: the backup listing captures before testing", bool(frag2))
if frag2:
    body2 = frag2.group(0)
    none = sh(f'''
      DEST_REAL=$(mktemp -d); mkdir -p "$DEST_REAL/state"     # no backups at all
      {body2}
    ''')
    ok("sutando-migrate: no backups now SAYS '(none)'",
       "(none)" in none, f"got {none!r}")
    # CONTROL — a real backup must still be named. This is the branch an operator
    # reads to pick a --backup-id, so replacing it with "(none)" would be worse
    # than the bug.
    some = sh(f'''
      DEST_REAL=$(mktemp -d); mkdir -p "$DEST_REAL/state"
      : > "$DEST_REAL/state/migration-backup-20260802a.tar"
      {body2}
    ''')
    ok("CONTROL: sutando-migrate still names a real backup id",
       "20260802a" in some and "(none)" not in some, f"got {some!r}")


# --- no regression: the retired shape must not come back ---------------------
for path in ("skills/self-diagnose/scripts/gather-remote.sh", "scripts/sutando-migrate.sh"):
    src = (REPO / path).read_text()
    bad = [ln.strip() for ln in src.splitlines()
           if re.search(r'\|\s*(head|sed|awk|tr)\b[^|]*\|\|\s*(echo|printf|\{)', ln)]
    ok(f"{path}: no pipeline-then-printing-|| remains", not bad, f"{bad}")


print(f"unreachable-pipeline-fallbacks: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
