#!/usr/bin/env python3
"""tool-suites-check: the checker that guards every other instrument had no
suite of its own. This is it — focused on the `extras` mechanism, whose whole
purpose is that a MISSING declared suite must be loud rather than silent.

Run:  python3 skills/proactive-loop/scripts/tool-suites-check.test.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts" / "tool-suites-check.py"
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={Path(__file__).resolve().parents[1] / '.coveragerc'}"]
spec = importlib.util.spec_from_file_location("tsc", TOOL)
tsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tsc)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r} want {want!r}")
    else:
        print(f"  ok   {label}")


def scaffold(td, extras=None, suite_body="print('PASS')\n", make_suite=True):
    """A minimal workspace: scripts/ with one passing suite, plus optional extras."""
    ws = Path(td) / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "scripts" / "dummy.test.py").write_text("print('PASS — dummy')\n")
    repo = Path(td) / "repo"
    (repo / "tests").mkdir(parents=True)
    if make_suite:
        (repo / "tests" / "extra.test.py").write_text(suite_body)
    if extras is not None:
        (ws / "state" / "tool-suites-extra.json").write_text(json.dumps({"suites": extras}))
    return ws, repo


def run(ws, repo, *extra_argv):
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws),
                        "--repo", str(repo), "--force", *extra_argv],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


print("1. with no extras file the checker behaves exactly as before")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    rc, out = run(ws, repo)
    check("exit 0", rc, 0)
    check("ran the scripts/ suite", "dummy.test.py" in out, True)

print("2. a declared extra suite IS run")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"])
    rc, out = run(ws, repo)
    check("exit 0", rc, 0)
    check("extra suite ran", "extra.test.py" in out, True)
    check("counted in the total", "2 of 2 suites pass" in out, True)

print("3. a declared extra that FAILS fails the whole check")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"],
                        suite_body="import sys\nprint('boom')\nsys.exit(1)\n")
    rc, out = run(ws, repo)
    check("exit 1", rc, 1)
    check("names the failing suite", "extra.test.py" in out, True)

print("4. ⚠ THE DISCRIMINATING CASE — a declared suite that is MISSING must be LOUD.")
print("   Skipping it silently would report a clean bill for a suite that never ran,")
print("   which is the exact failure this checker exists to prevent.")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"], make_suite=False)
    rc, out = run(ws, repo)
    check("exit 2 (cannot answer), NOT 0", rc, 2)
    check("names the missing path", "extra.test.py" in out, True)
    check("does not claim suites passed", "suites pass" in out, False)

print("5. a malformed extras file cannot pass as 'no extras'")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text("{not json")
    check("unreadable -> exit 2", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text(json.dumps({"suites": "tests/x.py"}))
    check("bare string -> exit 2", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    # A dict whose KEY is a real suite path: without the isinstance(list) guard it
    # iterates to that key and runs. Only this input exercises the type guard.
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text(
        json.dumps({"suites": {"tests/extra.test.py": True}}))
    check("dict-with-valid-key -> exit 2, not a silent run", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=[""])
    check("empty entry -> exit 2", run(ws, repo)[0], 2)

print("6. an extra suite joins the CHANGE trigger, not just the run set")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"])
    run(ws, repo)                                    # seed the sentinel (--force)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("second run is fresh", "fresh" in p.stdout, True)
    os.utime(repo / "tests" / "extra.test.py", None)  # touch ONLY the extra
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("touching the extra re-triggers", "running" in p.stdout, True)

def watched_dir_case(td):
    """A workspace whose tools live in tools/, with an EMPTY scripts/ beside it.

    The empty dir is the trap: it exists, so the `no scripts/` refusal does not
    fire, and it holds no .py, so the newest-mtime trigger has nothing to watch.
    """
    ws = Path(td) / "ws3"
    (ws / "scripts").mkdir(parents=True)          # exists, deliberately empty
    (ws / "tools").mkdir()
    (ws / "state").mkdir()
    (ws / "tools" / "thing.py").write_text("def verdict():\n    return 1\n")
    (ws / "tools" / "thing.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = watched_dir_case(td)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("tools/ is watched when scripts/ is empty", "running" in p.stdout, True)
    check("and the suite under tools/ actually ran", "thing.test.py" in p.stdout, True)
    # The trigger must still key on an edit, not merely on the first run.
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("second run is fresh", "fresh" in p.stdout, True)
    os.utime(ws / "tools" / "thing.py", None)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("touching a tool under tools/ re-triggers", "running" in p.stdout, True)


def both_populated_case(td):
    """BOTH scripts/ and tools/ hold .py — the ordinary mid-migration state.

    `next(...)` returned scripts/ and stopped, so tools/ was neither watched nor
    DISCOVERED: its suites never ran at all (3852-r1). Healthy from inside,
    because the selected dir still yields a non-empty runnable set.
    """
    ws = Path(td) / "ws5"
    for d in ("scripts", "tools", "state"):
        (ws / d).mkdir(parents=True)
    (ws / "scripts" / "legacy.py").write_text("def verdict():\n    return 1\n")
    (ws / "scripts" / "legacy.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    (ws / "tools" / "newer.py").write_text("def verdict():\n    return 2\n")
    (ws / "tools" / "newer.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = both_populated_case(td)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("both dirs populated: the scripts/ suite runs", "legacy.test.py" in p.stdout, True)
    check("...and the tools/ suite runs too (next() dropped it)", "newer.test.py" in p.stdout, True)
    check("...counted as 2, so neither is silently outside the set",
          "2 of 2 suites pass" in p.stdout, True)
    # The trigger must key on an edit in EITHER dir, not only the first one.
    subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                   capture_output=True, text=True)
    os.utime(ws / "tools" / "newer.py", None)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("touching a tool in the UNSELECTED dir re-triggers", "running" in p.stdout, True)


def third_dir_case(td):
    """A THIRD layout: scripts/ populated, tools/ absent, tools also in bin/.

    Reported by jsun-m on another host (3852-r3), measured not inferred: the
    fixed `(scripts, tools)` pair resolves on the populated scripts/ and stops,
    so four tools under bin/ are never stat'd. They broke one of them and the
    sweep still printed `fresh` — the same invisible-from-inside shape this PR
    exists to fix, with scripts/ populated instead of empty.
    """
    ws = Path(td) / "ws9"
    for d in ("scripts", "bin", "state"):
        (ws / d).mkdir(parents=True)
    (ws / "scripts" / "kept.py").write_text("def verdict():\n    return 1\n")
    (ws / "scripts" / "kept.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    (ws / "bin" / "elsewhere.py").write_text("def verdict():\n    return 2\n")
    (ws / "bin" / "elsewhere.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = third_dir_case(td)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("a third dir's suite is DISCOVERED, not just unwatched", "elsewhere.test.py" in p.stdout, True)
    check("...alongside the conventional one", "kept.test.py" in p.stdout, True)
    check("...counted as 2, so neither sits silently outside the set",
          "2 of 2 suites pass" in p.stdout, True)
    # The reported symptom: an edit in the third dir left the trigger blind.
    subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                   capture_output=True, text=True)
    os.utime(ws / "bin" / "elsewhere.py", None)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("touching a tool in the THIRD dir re-triggers", "running" in p.stdout, True)

with tempfile.TemporaryDirectory() as td:
    # Discovery must stay bounded: a dir with no .py is not watched, and dotted
    # dirs are skipped so a vendored tree cannot enlarge the sweep.
    ws = Path(td) / "ws10"
    for d in ("tools", "state", "notes", ".hidden"):
        (ws / d).mkdir(parents=True)
    (ws / "tools" / "t.py").write_text("x = 1\n")
    (ws / "notes" / "readme.md").write_text("no python here\n")
    (ws / ".hidden" / "h.py").write_text("x = 1\n")
    names = [d.name for d in tsc.watched_dirs(ws)]
    check("a dir with no .py is not watched", "notes" in names, False)
    check("a dotted dir is skipped", ".hidden" in names, False)
    check("the dir that does hold .py IS watched", "tools" in names, True)
    check("a missing workspace yields no candidates, not a crash",
          tsc.watched_dirs(Path(td) / "nope"), [])


print("7. in-process: the trigger rule and the two scope refusals")
check("no recorded run -> run", tsc.should_run({}, 0.0, 3600, 100.0)[0], True)
check("unchanged and young -> fresh", tsc.should_run({"tools_mtime": 5.0, "ran_at": 90.0}, 5.0, 3600, 100.0)[0], False)
go, why = tsc.should_run({"tools_mtime": 5.0, "ran_at": 0.0}, 5.0, 3600, 7200.0)
check("unchanged but older than --max-age -> run", (go, "last run was" in why), (True, True))

# A red run stamps `ran_at`/`tools_mtime` like any other, so without reading the
# recorded failure list the next call reports fresh and exits 0 on a red tree.
go, why = tsc.should_run(
    {"tools_mtime": 5.0, "ran_at": 90.0, "failed": ["a.test.py"]}, 5.0, 3600, 100.0)
check("a previous run with FAILURES re-runs, though nothing changed",
      (go, "failing" in why), (True, True))
check("CONTROL: the same state with an EMPTY failed list is fresh",
      tsc.should_run({"tools_mtime": 5.0, "ran_at": 90.0, "failed": []},
                     5.0, 3600, 100.0)[0], False)
check("CONTROL: and with no `failed` key at all it is still fresh",
      tsc.should_run({"tools_mtime": 5.0, "ran_at": 90.0}, 5.0, 3600, 100.0)[0], False)
with tempfile.TemporaryDirectory() as td:
    check("no scripts/ dir -> exit 2", tsc.main(["--workspace", td]), 2)
    (Path(td) / "scripts").mkdir()
    check("zero suites -> exit 2 (a scope result, not a clean bill)", tsc.main(["--workspace", td]), 2)


# 8. EXTRAS-ONLY: no local *.py; refusing on an empty candidate list skipped
#    all 10 declared suites on a real host (3852-r2).
print("8. extras-only: no local *.py, suites come entirely from the extras file")
with tempfile.TemporaryDirectory() as td:
    ws, repo = Path(td) / "ws8", Path(td) / "repo"
    (ws / "scripts").mkdir(parents=True)          # exists, 0 *.py — and no tools/
    (ws / "state").mkdir()
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "declared.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    (ws / "state" / "tool-suites-extra.json").write_text(
        '{"suites": ["tests/declared.test.py"]}')
    p2 = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                        capture_output=True, text=True)
    check("extras-only workspace still RUNS (does not refuse on empty candidates)",
          p2.returncode == 0 and "running" in p2.stdout, True)
    check("...and the declared suite is the one that ran",
          "declared.test.py" in p2.stdout, True)
    # and with the extras file removed there is genuinely nothing -> refuse
    (ws / "state" / "tool-suites-extra.json").unlink()
    p2 = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                        capture_output=True, text=True)
    check("no local *.py AND no extras -> still exit 2", p2.returncode, 2)

def stale_bytecode_case(td):
    """A tool the suite imports, run once, then edited to the same length.

    A same-size edit in the same second is served the pre-edit bytecode, so the
    suite asserts against code that is not on disk and still exits 0.
    """
    ws = Path(td) / "ws2"
    (ws / "scripts").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "scripts" / "thing.py").write_text('def verdict():\n    return "AAAA"\n')
    (ws / "scripts" / "thing.test.py").write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "p = Path(__file__).with_name('thing.py')\n"
        "s = importlib.util.spec_from_file_location('thing', p)\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "print('verdict', m.verdict())\n"
        "sys.exit(0 if m.verdict() == 'AAAA' else 1)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = stale_bytecode_case(td)
    args = ["--workspace", str(ws), "--repo", str(Path(td))]
    r1 = subprocess.run(PYBASE + [str(TOOL)] + args, capture_output=True, text=True)
    check("stale-bytecode: first run passes (the tool really does say AAAA)", r1.returncode, 0)

    tool = ws / "scripts" / "thing.py"
    tool.write_text(tool.read_text().replace('"AAAA"', '"BBBB"'))
    check("stale-bytecode: the edit is on disk", '"BBBB"' in tool.read_text(), True)

    r2 = subprocess.run(PYBASE + [str(TOOL)] + args, capture_output=True, text=True)
    # Without a fresh cache the suite imports the PRE-edit module, still sees
    # AAAA, and this run reports 0 — green on code that no longer exists.
    check("stale-bytecode: the edited tool is seen, so the suite FAILS",
          r2.returncode, 1)
    check("stale-bytecode: and the failure names the suite",
          "thing.test.py" in (r2.stdout + r2.stderr), True)


print("\ncase: the extras declaration resolves to a VAULT-CARRIED path")
# The vault carries hosts/*/ and not state/, so a declaration left under state/
# is unbacked-up; losing it disables its suites with a green exit.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "state").mkdir(parents=True)
    (ws / "hosts" / "H").mkdir(parents=True)
    decl = json.dumps({"suites": []})
    check("neither present -> the host path is proposed",
          tsc.extras_path(ws, "H").parent.name, "H")
    (ws / "state" / tsc.EXTRAS).write_text(decl)
    check("only the legacy copy -> still read (an un-migrated host keeps its extras)",
          tsc.extras_path(ws, "H").parent.name, "state")
    (ws / "hosts" / "H" / tsc.EXTRAS).write_text(decl)
    check("both present -> the carried copy wins",
          tsc.extras_path(ws, "H").parent.name, "H")
    check("an unresolvable host -> the legacy copy, never a hosts/None path",
          tsc.extras_path(ws, None).parent.name, "state")

print("\ncase: a migrated host with NO environment override still finds its extras")
# The documented invocation passes no --host, so an env-only default drops a
# migrated host to the absent state/ copy and exits green on zero extras.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "dummy.test.py").write_text("print('PASS')\n")
    repo = Path(td) / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "declared.test.py").write_text("print('PASS')\n")
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "util_paths.py").write_text(
        "def _host_label():\n    return 'RESOLVED-HOST'\n")
    (ws / "hosts" / "RESOLVED-HOST").mkdir(parents=True)
    (ws / "hosts" / "RESOLVED-HOST" / tsc.EXTRAS).write_text(
        json.dumps({"suites": ["tests/declared.test.py"]}))
    env = dict(os.environ); env.pop("SUTANDO_HOST_LABEL", None)
    r = subprocess.run([sys.executable, str(TOOL), "--workspace", str(ws),
                        "--repo", str(repo), "--force"],
                       capture_output=True, text=True, env=env)
    check("the declared suite actually ran", "declared.test.py" in r.stdout, True)
    check("it is counted, not silently dropped", "2 of 2 suites pass" in r.stdout, True)
    check("exit 0", r.returncode, 0)

print("\ncase: the uncarried warning names a remedy that can actually work")
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "state").mkdir(parents=True)
    f = ws / "state" / tsc.EXTRAS
    f.write_text(json.dumps({"suites": []}))
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        tsc.extra_suites(f, Path(td))
    msg = buf.getvalue()
    check("it warns at all", "NOT tracked" in msg, True)
    check("it points at hosts/<host>/", "hosts/<host>/" in msg, True)
    # The old remedy was a bare "Add its path to vault.sync.include". A whitelist
    # re-include DOES work in general; it fails here for two other reasons.
    check("it does not prescribe a bare include entry as the fix",
          "Add its path to vault.sync.include." in msg, False)
    check("it names the carve-out ordering", "after includes" in msg, True)
    check("it warns that include REPLACES the carrier set", "REPLACES" in msg, True)

print("\ncase: a state DIRECTORY still resolves (the pre-move call shape)")
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "state").mkdir(parents=True)
    repo = Path(td) / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "e.test.py").write_text("print('PASS')\n")
    (ws / "state" / tsc.EXTRAS).write_text(json.dumps({"suites": ["tests/e.test.py"]}))
    got = [x.name for x in tsc.extra_suites(ws / "state", repo)]
    check("a directory argument is still accepted", got, ["e.test.py"])

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nPASS — tool-suites-check tests")
