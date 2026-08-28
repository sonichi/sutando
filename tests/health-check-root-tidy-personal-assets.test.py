#!/usr/bin/env python3
"""workspace-root-tidy must not flag a file the personal-asset resolver reads there.
The root is that resolver's last step, so a "move it to state/" remedy breaks it."""
import fnmatch
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("REPO_UNDER_TEST") or Path(__file__).resolve().parent.parent)

#: Names resolved through the same helper but held as DIRECTORIES, which the
#: probe never inspects (it walks files only). A new entry here needs a reason.
DIRECTORY_VALUED = {"notes"}

fails = 0


def ok(msg):
    print(f"  ok   {msg}")


def bad(msg, why=""):
    global fails
    print(f"  FAIL {msg} — {why}")
    fails += 1


def load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def derive_personal_path_names():
    """Filenames reachable through the personal-asset resolver. The shell call sites
    hide in python heredocs, which neither lint-class-rules.py extractor reaches."""
    lcr = load("lcr", "scripts/lint-class-rules.py")
    names = set()
    for sub in ("src", "scripts", "skills"):
        d = REPO / sub
        if not d.is_dir():
            continue
        names |= set(lcr.extract_personal_path_args_py(d))
        names |= set(lcr.extract_personal_path_args_ts(d))
        names |= shell_heredoc_names(d)
    return names


def shell_heredoc_names(d: Path) -> set:
    pat = re.compile(r"""\bpersonal_path\(\s*['"]([^'"]+)['"]""")
    names = set()
    for f in sorted(d.rglob("*.sh")):
        if not f.is_file():
            continue
        for line in f.read_text(errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            names |= set(pat.findall(line))
    return names


print("workspace-root-tidy vs personal-asset resolution:")

hc = load("hc", "src/health-check.py")

derived = derive_personal_path_names()
if not derived:
    bad("derived at least one personal_path name from the sources",
        "found none — the extractors no longer match the call sites, so this "
        "test would pass vacuously")
else:
    ok(f"derived {len(derived)} personal-asset name(s) from src/, scripts/, skills/")

# Files (not directories) whose workspace-root copy is a legitimate read target.
root_files = sorted(derived - DIRECTORY_VALUED)

personal_assets = getattr(hc, "WORKSPACE_ROOT_PERSONAL_ASSETS", None)
if personal_assets is None:
    unguarded = [n for n in root_files if n not in hc.WORKSPACE_ROOT_ALLOWED]
    bad("health-check.py defines WORKSPACE_ROOT_PERSONAL_ASSETS",
        "not defined — the tidy probe has no exemption for personal assets, so "
        "every name below is reported as contract drift with a remedy "
        f"(move to state/) that breaks personal_path(): {', '.join(unguarded)}")
    personal_assets = frozenset()
else:
    ok("health-check.py defines WORKSPACE_ROOT_PERSONAL_ASSETS")

exempt = set(hc.WORKSPACE_ROOT_ALLOWED) | set(personal_assets)
missing = [
    n for n in root_files
    if n not in exempt
    and not any(fnmatch.fnmatch(n, g) for g in hc.WORKSPACE_ROOT_SENTINEL_GLOBS)
]
if missing:
    bad("every root-resolved personal asset is exempt from the tidy probe",
        f"not exempt: {', '.join(missing)} — either add to "
        f"WORKSPACE_ROOT_PERSONAL_ASSETS or, if it is a directory, to this "
        f"test's DIRECTORY_VALUED with a reason")
else:
    ok("every root-resolved personal asset is exempt from the tidy probe")

# The probe end to end, seeded from the DERIVED names: seeding from the exemption
# set would pass vacuously on a tree that has no constant, and so no exemption.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    (ws / "state").mkdir(parents=True)
    for name in root_files:
        (ws / name).write_text("x")
    hc.WORKSPACE_DIR = ws
    verdict = hc.check_workspace_root_tidy()
    if verdict is None:
        ok("a root holding only personal assets reports clean")
    else:
        bad("a root holding only personal assets reports clean",
            f"warned instead: {verdict.get('detail', '')[:200]}")

    # The probe must still catch real drift; an exemption that swallows
    # everything would pass the check above just as well.
    (ws / "voice-state.json").write_text("{}")
    verdict = hc.check_workspace_root_tidy()
    if verdict and "voice-state.json" in verdict.get("detail", ""):
        ok("genuine drift alongside personal assets is still reported")
    else:
        bad("genuine drift alongside personal assets is still reported",
            f"got {verdict}")

if fails == 0:
    print("ALL PASS")
    sys.exit(0)
print(f"FAILED ({fails})")
sys.exit(1)
