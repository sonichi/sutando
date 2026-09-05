#!/usr/bin/env python3
"""Re-run the workspace tool suites when a tool has changed — or once a day.

WHY: these suites guard the instruments every proactive pass quotes (merge-gate,
the dedup checker, the triage gate, the memory-index guard). Nothing re-ran them.
Measured 2026-09-01: two were broken and nobody knew. `notify_reviewers.py` did
not even PARSE — I broke it that morning marking it superseded, and its suite was
not run afterwards. `merge-gate-gate.test.py` had been dying at check 3 of 124 on
a fixture that drifted behind the code, so 121 assertions on the instrument every
shepherd sweep quotes had gone unexecuted.

THE TRIGGER IS THE POINT. A daily cadence alone would still have let that morning's
edit sit green until the next day; the condition that mattered is "a tool changed
since the last green run". So this runs when any tool or suite is NEWER than the
last recorded run, and otherwise only after --max-age. On an unchanged tree it is
two stat() calls and prints `fresh`.

⚠ Suites are invoked with NO extra argv. A unittest-based suite reads argv[1] as a
test-NAME selector, so passing a repo path makes it error with
`AttributeError: module '__main__' has no attribute '<repo path>'` — which is
indistinguishable from a real failure until you vary the harness. That cost four
false failures the first time this sweep was run by hand.

exit 0 all pass (or fresh) · 1 a suite failed · 2 cannot answer
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import sys
import time
from pathlib import Path

SENTINEL = "tool-suites-last-run.json"
EXTRAS = "tool-suites-extra.json"       # {"suites": ["tests/x.test.py", ...]}



def watched_dirs(ws: Path):
    """Every immediate subdir of the workspace holding a top-level *.py.

    Named dirs were the bug: `(scripts, tools)` misses a host whose tools live in
    `bin/`, and the trigger then reports fresh forever while that dir's tools go
    unstat'd. Over-watching costs a stat; under-watching costs a gate that is
    green by construction.
    """
    if not ws.is_dir():
        return []
    return sorted(
        (d for d in ws.iterdir()
         if d.is_dir() and not d.name.startswith(".") and any(d.glob("*.py"))),
        key=lambda d: d.name,
    )


def tools_and_suites(dirs):
    """Union over EVERY candidate dir, not the first that matches: a workspace
    mid-migration holds .py in both, and picking one hides the other's suites."""
    found = [p for d in dirs for p in d.glob("*.py")]
    suites = sorted(p for p in found if p.name.endswith(".test.py"))
    tools = sorted(p for p in found if not p.name.endswith(".test.py"))
    return tools, suites


class ExtrasError(Exception):
    """A declared extra suite could not be resolved."""


def extra_suites(statedir: Path, repo: Path):
    """Suites living OUTSIDE the workspace scripts dir, declared explicitly.

    A locally-deployed guard (e.g. a PreToolUse hook) keeps its suite with the
    code it guards, so the scripts/ glob cannot see it — and an uncommitted one
    is not covered by CI either, so nothing runs it at all. Declaring it here
    puts it under the same changed-since-last-green trigger.

    A declared path that does not exist RAISES. Skipping it silently would let
    a typo or a moved file report a clean bill for a suite that never ran,
    which is the exact failure this whole check exists to prevent.
    """
    f = statedir / EXTRAS
    if not f.is_file():
        return []
    try:
        decl = json.loads(f.read_text()).get("suites", [])
    except Exception as e:
        raise ExtrasError(f"{f} is unreadable: {e}") from e
    if not isinstance(decl, list):
        raise ExtrasError(f"{f}: 'suites' must be a list, got {type(decl).__name__}")
    out, missing = [], []
    for item in decl:
        if not isinstance(item, str) or not item.strip():
            raise ExtrasError(f"{f}: every entry must be a non-empty string")
        cand = Path(item) if os.path.isabs(item) else (repo / item)
        (out if cand.is_file() else missing).append(cand)
    if missing:
        raise ExtrasError("declared extra suite(s) not found: "
                          + ", ".join(str(m) for m in missing))
    _warn_if_uncarried(f)
    return sorted(out)


def _warn_if_uncarried(decl: Path) -> None:
    """Warn if THIS declaration is not backed up by the vault.

    Losing it is silent and disabling: absent means 'no extras' by design, so
    the suites it registers stop running and this check still exits 0. That is
    the same shape as the gap the extras mechanism exists to close, arriving
    through the backup path instead. Measured 2026-09-01: written under state/,
    which carries only current-track.md, so it was untracked from birth.

    Advisory only — a backup concern must never fail a test run.
    """
    try:
        r = subprocess.run(["git", "-C", str(decl.parent.parent), "ls-files", "--error-unmatch",
                            str(decl.relative_to(decl.parent.parent))],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return                      # no git, no repo, or a path outside it: not our business
    if r.returncode != 0:
        print(f"[tool-suites-check] WARNING: {decl} is NOT tracked in the workspace vault. "
              f"If it is lost, the suites it registers stop running SILENTLY (absent = no extras). "
              f"Add its path to vault.sync.include.", file=sys.stderr)


def newest_mtime(paths) -> float:
    return max((p.stat().st_mtime for p in paths), default=0.0)


def should_run(state: dict, newest: float, max_age: float, now: float) -> "tuple[bool, str]":
    if not state:
        return True, "no previous run recorded"
    if newest > state.get("tools_mtime", 0.0):
        return True, "a tool or suite changed since the last green run"
    age = now - state.get("ran_at", 0.0)
    if age > max_age:
        return True, f"last run was {age/3600:.1f}h ago (> {max_age/3600:.0f}h)"
    return False, f"fresh — unchanged and last run {age/3600:.1f}h ago"


def run_suites(suites, cwd) -> "list[tuple[str, int, str]]":
    out = []
    # A fresh bytecode cache per run: an edited tool can otherwise be served its
    # PRE-edit .pyc and report green. `-B` stops writing, not reading.
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="tool-suites-pyc-") as pycache:
        env["PYTHONPYCACHEPREFIX"] = pycache
        out.extend(_run_each(suites, cwd, env))
    return out


def _run_each(suites, cwd, env) -> "list[tuple[str, int, str]]":
    out = []
    for s in suites:
        try:
            # NO extra argv: unittest would read it as a test-name selector.
            r = subprocess.run([sys.executable, str(s)], capture_output=True,
                               text=True, timeout=180, cwd=cwd, env=env)
            lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
            last = lines[-1] if lines else (r.stderr.strip().splitlines() or ["(no output)"])[-1]
            out.append((s.name, r.returncode, last[:100]))
        except subprocess.TimeoutExpired:
            out.append((s.name, 124, "TIMEOUT 180s"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo", default=".", help="cwd for the suites (some import from src/)")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    ws = Path(a.workspace)
    statedir = ws / "state"
    # DISCOVER the dirs; never name them. A fixed list is the same defect one
    # name later — a third dir is unwatched and its staleness is silent (3852-r3).
    candidates = watched_dirs(ws)
    tools, suites = tools_and_suites(candidates)
    try:
        extras = extra_suites(statedir, Path(a.repo).resolve())
    except ExtrasError as e:
        print(f"CANNOT ANSWER: {e}", file=sys.stderr)
        return 2
    suites = suites + extras
    if not suites:
        print("CANNOT ANSWER: zero suites found — that is a scope result, not a clean bill",
              file=sys.stderr)
        return 2

    sf = statedir / SENTINEL
    state = json.loads(sf.read_text()) if sf.is_file() else {}
    now = time.time()
    newest = newest_mtime(tools + suites)
    go, why = should_run(state, newest, a.max_age_hours * 3600, now)
    if a.force:
        go, why = True, "--force"
    if not go:
        print(f"fresh — {len(suites)} suite(s) skipped ({why})")
        return 0

    print(f"running {len(suites)} suite(s): {why}")
    results = run_suites(suites, a.repo)
    bad = [(n, rc, last) for n, rc, last in results if rc != 0]
    for n, rc, last in results:
        print(f"  {'ok  ' if rc == 0 else 'FAIL'}  {n:<42} {last[:56]}")
    print(f"\n{len(results) - len(bad)} of {len(results)} suites pass")
    if bad:
        print("\nFAILING — the instruments these guard are quoted every pass:")
        for n, rc, last in bad:
            print(f"  {n} (rc={rc}): {last}")

    statedir.mkdir(exist_ok=True)
    tmp = sf.with_suffix(sf.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "ran_at": now, "tools_mtime": newest,
        "suites": len(results), "failed": [n for n, _, _ in bad],
    }, indent=2, sort_keys=True))
    os.replace(tmp, sf)          # record even on failure: the run happened
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
