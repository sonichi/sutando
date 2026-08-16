#!/usr/bin/env python3
"""An unmeasurable growth trend must say so, not return "" (#2958).

`_index_growth_note` builds its series from the index file's git history. On a
host whose memory dir is not version-controlled — a shipped, supported
single-machine configuration — every path returns "", and `memory-index` renders
a clean `ok` with no growth clause. Absence then reads exactly like "computed it,
nothing to report", so the projection that warns about approaching the 25KB cut
is simply not running and nothing says so.

Same principle the prose-cap gate applies: never a bare PASS when nothing was read.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "src" / "health-check.py"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got {got!r}, want {want!r}")
        fails.append(name)


def load():
    spec = importlib.util.spec_from_file_location("hc_trend", MOD)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


m = load()
MARK = "growth trend unavailable"


def index_in(dirpath, size, gitted, revisions=1):
    """Write an index file; optionally give it a git history of `revisions`."""
    d = Path(dirpath)
    idx = d / "MEMORY.md"
    if not gitted:
        idx.write_text("- [x](x.md) — " + "y" * size + "\n")
        return idx
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    for i in range(revisions):
        idx.write_text("- [x](x.md) — " + "y" * (size - (revisions - 1 - i) * 4000) + "\n")
        env = dict(os.environ,
                   GIT_AUTHOR_DATE=str(int(time.time()) - (revisions - i) * 3600),
                   GIT_COMMITTER_DATE=str(int(time.time()) - (revisions - i) * 3600))
        subprocess.run(["git", "-C", str(d), "add", "MEMORY.md"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", f"c{i}"], env=env, check=True)
    return idx


def note_for(**kw):
    with tempfile.TemporaryDirectory() as td:
        idx = index_in(td, **kw)
        eff = len(m._index_effective_text(idx.read_text()).encode())
        return m._index_growth_note(idx, eff)


# 1. THE DEFECT: no git history at all -> must say so, not go silent.
n = note_for(size=16000, gitted=False)
check("an unversioned index reports the trend as unavailable", MARK in n, True)
check("...and does not return an empty string", n == "", False)

# 2. Only one revision — history exists but cannot form a trend. Same answer:
#    the operator still cannot see a projection, and still needs to know why.
n1 = note_for(size=16000, gitted=True, revisions=1)
check("a single-revision history also reports unavailable", MARK in n1, True)

# 3. CONTROL — a real multi-revision history must produce a REAL trend and must
#    NOT carry the marker. Without this, returning the marker unconditionally
#    would satisfy every assertion above.
n3 = note_for(size=16000, gitted=True, revisions=3)
check("a real history yields an actual measurement", "over the last" in n3, True)
check("...and never claims unavailable", MARK in n3, False)

# 4. The marker is a clause on an existing detail line, not a standalone
#    sentence — it is appended to `memory-index`'s ok detail, so it must start
#    with the same separator the real note uses.
check("the marker is a ';' clause, matching the real note's shape",
      n.lstrip().startswith(";"), True)

# 5. It must not read as a failure. This rides an `ok`; wording that looks like
#    an error would train operators to ignore a genuinely healthy line.
check("the marker contains no failure vocabulary",
      any(w in n.lower() for w in ("error", "fail", "broken", "warn")), False)

print(("FAILED: " + ", ".join(fails)) if fails else "index trend unavailable: all checks passed")
sys.exit(1 if fails else 0)
