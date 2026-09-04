#!/usr/bin/env python3
"""Tests for memory-index-budget.py. Run: python3 skills/proactive-loop/scripts/memory-index-budget.test.py

Fixtures are built against the REAL health-check limit rather than a mocked one,
because the thing under test is the delegation. A stubbed limit would pass with a
private copy of 25,000 hardcoded — the exact defect the guard refuses to have.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[0]
SCRIPT = REPO / "skills" / "proactive-loop" / "scripts" / "memory-index-budget.py"

spec = importlib.util.spec_from_file_location("mib", SCRIPT)
mib = importlib.util.module_from_spec(spec); spec.loader.exec_module(mib)

MOD = mib._health_check(REPO)
assert MOD is not None, f"health-check.py not importable from {REPO} — cannot run these tests"
LIMIT = MOD.MEMORY_INDEX_LOAD_BYTES

fails = []
ran = 0
def check(name, cond, detail=""):
    global ran; ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def row(i, pad=200):
    return f"- [row {i:04d}](feedback_row_{i:04d}.md) — {'x' * pad}\n"

def index_of(nbytes):
    """An index whose effective text is just under `nbytes`."""
    out, tot, i = [], 0, 0
    while tot + len(row(i).encode()) <= nbytes:
        out.append(row(i)); tot += len(row(i).encode()); i += 1
    return "".join(out)

print("memory-index-budget")

# --- the report mode -------------------------------------------------------
small = index_of(LIMIT // 2)
r = mib.evaluate(MOD, small)
check("report: a small index drops nothing", r["already_dropped"] == [])
check("report: bytes match the delegated measurement",
      r["bytes"] == mib.loaded_lines(MOD, small)[1])
check("report: the limit comes from health-check, not a local constant",
      r["limit"] == MOD.MEMORY_INDEX_LOAD_BYTES)

over = index_of(LIMIT) + row(9998) + row(9999)
r = mib.evaluate(MOD, over)
check("report: an over-budget index NAMES the rows already past the cut",
      len(r["already_dropped"]) >= 2, f"got {len(r['already_dropped'])}")

# --- adding: the two distinct failure modes --------------------------------
full = index_of(LIMIT)
r = mib.evaluate(MOD, full, row(5000))
check("append to a full index: the ADDITION is what fails to load",
      r["addition_loads"] is False and r["dropped"] == [],
      f"loads={r['addition_loads']} dropped={len(r['dropped'])}")

r = mib.evaluate(MOD, full, row(5000), at_top=True)
check("insert at top of a full index: an EXISTING row is dropped",
      len(r["dropped"]) >= 1 and r["addition_loads"] is True,
      f"dropped={len(r['dropped'])} loads={r['addition_loads']}")
check("the dropped row is named, not just counted",
      r["dropped"] and r["dropped"][0].startswith("- [row"), str(r["dropped"][:1]))

r = mib.evaluate(MOD, small, row(5000))
check("adding to a half-full index is safe",
      r["dropped"] == [] and r["addition_loads"] is True)

# --- strip delegation ------------------------------------------------------
commented = "<!-- " + "y" * (LIMIT // 2) + " -->\n" + small
r = mib.evaluate(MOD, commented, row(5000))
check("a huge HTML comment costs no budget (strip is delegated, not re-implemented)",
      r["dropped"] == [] and r["addition_loads"] is True,
      "raw-text measurement would blow the budget here")

# --- refusal ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    check("no health-check.py -> None, so main() can refuse", mib._health_check(pathlib.Path(d)) is None)
    rc = mib.main(["--repo", d, "--index", str(SCRIPT)])
    check("exit 2 when the authority is unreadable (never a hardcoded fallback)", rc == 2, f"rc={rc}")

with tempfile.TemporaryDirectory() as d:
    rc = mib.main(["--repo", str(REPO), "--index", str(pathlib.Path(d) / "nope.md")])
    check("exit 2 when the index is absent", rc == 2, f"rc={rc}")

# --- exit codes end to end -------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "MEMORY.md"; p.write_text(full)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_bad = mib.main(["--repo", str(REPO), "--index", str(p), "--adding", row(5000).strip()])
    p.write_text(small)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_ok = mib.main(["--repo", str(REPO), "--index", str(p), "--adding", row(5000).strip()])
    check("exit 1 refuses, exit 0 permits", (rc_bad, rc_ok) == (1, 0), f"got {(rc_bad, rc_ok)}")

# --- main(): report mode, --adding-file, --at-top, and the refusal prints ----
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "MEMORY.md"; p.write_text(over)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mib.main(["--repo", str(REPO), "--index", str(p)])
    check("report mode on an over-budget index exits 1 and names the rows",
          rc == 1 and "ALREADY NOT LOADING" in buf.getvalue(), f"rc={rc}")
    p.write_text(small)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mib.main(["--repo", str(REPO), "--index", str(p)])
    check("report mode on a healthy index exits 0", rc == 0 and "load (limit" in buf.getvalue(), f"rc={rc}")
    add = pathlib.Path(d) / "add.md"; add.write_text(row(7000))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mib.main(["--repo", str(REPO), "--index", str(p), "--adding-file", str(add)])
    check("--adding-file reads the addition and permits it", rc == 0 and "safe" in buf.getvalue(), f"rc={rc}")
    p.write_text(full)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mib.main(["--repo", str(REPO), "--index", str(p), "--adding", row(5000).strip(), "--at-top"])
    check("--at-top on a full index REFUSES and names the dropped row",
          rc == 1 and "REFUSE" in buf.getvalue() and "- [row" in buf.getvalue(), f"rc={rc}")

# --- corpus resolution: MEMORY_DIR is derived from cwd, not from what loads ----
# Live host: the MEMORY_DIR sibling read 45.8% while the loaded tree was 90.2%.

def _tree(projects, slug, text, age_s):
    d = projects / slug / "memory"
    d.mkdir(parents=True)
    m = d / "MEMORY.md"
    m.write_text(text)
    os.utime(m, (time.time() - age_s, time.time() - age_s))
    return d

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    stale = _tree(projects, "slug-stale", index_of(LIMIT // 3), age_s=86400)
    live = _tree(projects, "slug-live", index_of(LIMIT // 2), age_s=60)
    got, note = mib._live_index(stale)
    # A 12-day gap is exactly the case where "freshest" looks authoritative and
    # is not: an unrelated project edited later is still not what loads here.
    check("resolution: two corpora and no authoritative signal REFUSES, however wide the gap",
          got is None and "CANNOT ANSWER" in note, f"got={got}")
    check("resolution: the refusal NAMES the candidates rather than picking one",
          "slug-live" in note and "slug-stale" in note, f"note={note!r}")
    check("resolution: the refusal says what to do instead of leaving the caller stuck",
          "SUTANDO_MEMORY_DIR" in note and "--index" in note, f"note={note!r}")

# The pointer lives at <workspace>/hosts/<label>/memory-corpus; pin the label so
# the test does not depend on the machine it runs on.
def _ptr_for(projects):
    os.environ["SUTANDO_HOST_LABEL"] = "test-host"
    return mib._host_pointer_path(projects)

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "ws" / ".claude-sutando" / "projects"
    stale = _tree(projects, "slug-stale", index_of(LIMIT // 3), age_s=86400)
    live = _tree(projects, "slug-live", index_of(LIMIT // 2), age_s=60)
    ptr = _ptr_for(projects)
    check("pointer: it is per-host by construction, not a shared filename",
          ptr.parent.name == "test-host" and ptr.parent.parent.name == "hosts", f"ptr={ptr}")
    # The pointer adds a way in; it does not soften the ambiguous refusal.
    got, note = mib._live_index(stale)
    check("pointer: absent pointer leaves the ambiguous refusal intact",
          got is None and "CANNOT ANSWER" in note, f"got={got}")
    check("pointer: the refusal now names the file to record, so a host is not stuck",
          str(ptr) in note and "RECORD IT ONCE" in note, f"note={note!r}")
    # Point at the corpus that is neither freshest nor the derived one, so only
    # the pointer can produce this answer.
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(str(live / "MEMORY.md") + "\n")
    got, note = mib._live_index(stale)
    check("pointer: a recorded corpus resolves, outranking both cwd and freshness",
          got == live / "MEMORY.md" and note == "", f"got={got} note={note!r}")
    # Falling through here would let a typo silently measure a different corpus.
    ptr.write_text(str(projects / "no-such" / "memory" / "MEMORY.md"))
    got, note = mib._live_index(stale)
    check("pointer: a pointer to a missing file REFUSES, never falls back to inference",
          got is None and "is not a file" in note, f"got={got} note={note!r}")
    ptr.write_text("   \n")
    got, note = mib._live_index(stale)
    check("pointer: an empty pointer is 'unrecorded', not 'recorded as nothing'",
          got is None and "CANNOT ANSWER" in note and "RECORD IT ONCE" in note, f"note={note!r}")
    os.environ.pop("SUTANDO_HOST_LABEL", None)

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    only = _tree(projects, "slug-only", index_of(LIMIT // 3), age_s=60)
    got, note = mib._live_index(only)
    check("resolution: a single corpus is unchanged behaviour and says nothing",
          got == only / "MEMORY.md" and note == "", f"got={got} note={note!r}")

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    a = _tree(projects, "slug-a", index_of(LIMIT // 3), age_s=10)
    _tree(projects, "slug-b", index_of(LIMIT // 3), age_s=11)
    got, note = mib._live_index(a)
    check("resolution: near-simultaneous indexes REFUSE too -- the gap never decides",
          got is None and "CANNOT ANSWER" in note, f"got={got} note={note!r}")

with tempfile.TemporaryDirectory() as d:
    # A fresher tree OUTSIDE the projects/ parent must not be reachable: the scope
    # bound is what keeps this from wandering the filesystem.
    projects = pathlib.Path(d) / "projects"
    mine = _tree(projects, "slug-mine", index_of(LIMIT // 3), age_s=3600)
    outside = pathlib.Path(d) / "elsewhere" / "memory"
    outside.mkdir(parents=True)
    (outside / "MEMORY.md").write_text(index_of(LIMIT // 2))
    got, _ = mib._live_index(mine)
    check("resolution: a fresher index OUTSIDE projects/ is not eligible",
          got == mine / "MEMORY.md", f"got {got}")

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    stale = _tree(projects, "slug-stale2", index_of(LIMIT // 3), age_s=86400)
    _tree(projects, "slug-live2", index_of(LIMIT // 2), age_s=60)
    named = stale / "MEMORY.md"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mib.main(["--repo", str(REPO), "--index", str(named)])
    check("--index still wins: an explicit path is never second-guessed",
          rc == 0 and "note: measuring" not in buf.getvalue(), f"rc={rc}")

with tempfile.TemporaryDirectory() as d:
    # The override outranks every heuristic, including when it names the OLDER
    # tree: the owner states identity, the tool does not infer it.
    projects = pathlib.Path(d) / "projects"
    older = _tree(projects, "slug-older", index_of(LIMIT // 3), age_s=86400)
    newer = _tree(projects, "slug-newer", index_of(LIMIT // 2), age_s=60)
    _prev = os.environ.get("SUTANDO_MEMORY_DIR")
    os.environ["SUTANDO_MEMORY_DIR"] = str(older)
    try:
        got, note = mib._live_index(older)
        check("resolution: with the override set, the caller's memory_dir is trusted outright",
              got == older / "MEMORY.md" and note == "", f"got={got} note={note!r}")
        got, note = mib._live_index(newer)
        check("resolution: an override set means NO sibling scan -- a fresher tree is ignored",
              got == newer / "MEMORY.md" and note == "", f"got={got} note={note!r}")
        got, note = mib._live_index(projects / "slug-absent" / "memory")
        check("resolution: override set but the index missing REFUSES, never falls back to a scan",
              got is None and "CANNOT ANSWER" in note, f"got={got} note={note!r}")
    finally:
        if _prev is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = _prev

# --- the branches the coverage gate flagged: refusal, fallback, and main()'s default -
with tempfile.TemporaryDirectory() as d:
    # No candidates under projects/, but MEMORY_DIR's own index exists -> use it, quietly.
    md = pathlib.Path(d) / "projects" / "only" / "memory"
    md.mkdir(parents=True)
    (md / "MEMORY.md").write_text(index_of(LIMIT // 3))
    empty = pathlib.Path(d) / "elsewhere" / "memory"
    empty.mkdir(parents=True)
    (empty / "MEMORY.md").write_text(index_of(LIMIT // 3))
    got, note = mib._live_index(empty)          # its projects/ parent holds no */memory/MEMORY.md
    check("fallback: no candidates under projects/ but MEMORY_DIR has one -> use it",
          got == empty / "MEMORY.md" and note == "", f"got={got} note={note!r}")

with tempfile.TemporaryDirectory() as d:
    missing = pathlib.Path(d) / "nowhere" / "memory"
    got, note = mib._live_index(missing)
    check("refusal: no candidates and no index at MEMORY_DIR -> None + CANNOT ANSWER",
          got is None and "CANNOT ANSWER" in note, f"got={got} note={note!r}")

with tempfile.TemporaryDirectory() as d:
    # Injected, not filesystem-induced: a FILE where projects/ should be does not
    # raise on macOS, it yields nothing — the wrong branch, same visible outcome.
    projects = pathlib.Path(d) / "projects"
    live = _tree(projects, "slug-os", index_of(LIMIT // 3), age_s=60)
    real_glob = pathlib.Path.glob
    def boom(self, pattern, *a, **k):
        if "memory/MEMORY.md" in pattern:
            raise OSError("injected: scan failed")
        return real_glob(self, pattern, *a, **k)
    pathlib.Path.glob = boom
    try:
        got, note = mib._live_index(live)
    finally:
        pathlib.Path.glob = real_glob
    # A raised scan proves nothing about uniqueness, so falling back to MEMORY_DIR's
    # own index would re-open the exact hole this resolver closes.
    check("an OSError from the scan REFUSES rather than falling back to the cwd default",
          got is None and "CANNOT ANSWER" in note and "uniqueness" in note,
          f"got={got} note={note!r}")
    check("the scan-failure refusal says how to proceed",
          "--index" in note and "SUTANDO_MEMORY_DIR" in note, f"note={note!r}")

def _main_with_memory_dir(memory_dir, args):
    """main() re-imports health-check, so SUTANDO_MEMORY_DIR is how MEMORY_DIR is set."""
    prev = os.environ.get("SUTANDO_MEMORY_DIR")
    os.environ["SUTANDO_MEMORY_DIR"] = str(memory_dir)
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = mib.main(args)
    finally:
        if prev is None: os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else: os.environ["SUTANDO_MEMORY_DIR"] = prev
    return rc, buf.getvalue(), err.getvalue()

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    stale = _tree(projects, "slug-stale3", index_of(LIMIT // 3), age_s=86400)
    _tree(projects, "slug-live3", index_of(LIMIT // 2), age_s=60)
    rc, out, _ = _main_with_memory_dir(stale, ["--repo", str(REPO)])
    # The helper sets the override, so main() measures what MEMORY_DIR names and
    # never scans siblings -- even with a fresher one beside it.
    check("main() honours the override and does NOT drift to the fresher sibling",
          rc == 0 and "slug-live3" not in out and "note: measuring" not in out,
          f"rc={rc} out={out[:120]!r}")

with tempfile.TemporaryDirectory() as d:
    projects = pathlib.Path(d) / "projects"
    a2 = _tree(projects, "slug-tie-a", index_of(LIMIT // 3), age_s=10)
    _tree(projects, "slug-tie-b", index_of(LIMIT // 3), age_s=11)
    rc, out, err = _main_with_memory_dir(a2, ["--repo", str(REPO)])
    # An override leaves no ambiguity to resolve; the refusal path needs it ABSENT,
    # which this harness cannot produce, so _live_index covers that directly above.
    check("main() with the override set resolves without ambiguity and does not refuse",
          rc == 0 and "CANNOT ANSWER" not in err, f"rc={rc} err={err[:100]!r}")

with tempfile.TemporaryDirectory() as d:
    # main()'s refusal path: reachable only when _live_index cannot resolve, which
    # the override CAN produce -- it names a directory holding no index.
    projects = pathlib.Path(d) / "projects"
    _tree(projects, "slug-present", index_of(LIMIT // 3), age_s=60)
    empty = projects / "slug-empty" / "memory"
    empty.mkdir(parents=True)
    rc, out, err = _main_with_memory_dir(empty, ["--repo", str(REPO)])
    check("main() REFUSES (rc 2) when the resolver cannot name an index, and says why on stderr",
          rc == 2 and "CANNOT ANSWER" in err and out == "", f"rc={rc} err={err[:90]!r} out={out[:40]!r}")

# --- the authority must be the real one: a stand-in that lacks the primitives is None
with tempfile.TemporaryDirectory() as d:
    (pathlib.Path(d) / "src").mkdir()
    hc = pathlib.Path(d) / "src" / "health-check.py"
    hc.write_text("raise SystemExit(0)\n")
    check("a health-check.py that exits on import (CLI) but lacks the primitives -> None",
          mib._health_check(pathlib.Path(d)) is None)
    hc.write_text("raise RuntimeError('broken')\n")
    check("a health-check.py that raises on import -> None", mib._health_check(pathlib.Path(d)) is None)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'} "
      f"({ran - len(fails)}/{ran} assertions)")
sys.exit(1 if fails else 0)
