#!/usr/bin/env python3
"""Run a test against several revisions of one file, printing what each ARM RESOLVED.

An arming run is evidence only if the arms differ. `git stash push <file>`
reverts to the CURRENT BRANCH's HEAD, not to the parent, so "parent vs HEAD" on
a feature branch silently runs the same bytes twice — and two arms reporting
identical numbers is exactly what a correct no-op change looks like, so the
artifact is indistinguishable from a real finding.

The fix is not care. It is printing the sha each arm resolved to, next to its
result, and refusing to call a comparison a comparison when two arms carry the
same blob. (Guard proposed by @yixuan-ag2 after I published a stashed
"parent" arm that was really HEAD.)

Usage:
  scripts/arm-test.py tests/x.test.py --file src/x.py --rev origin/main --rev HEAD
  scripts/arm-test.py tests/x.test.py --file src/x.py --rev origin/main   # + working tree

The working tree is always the final arm unless --no-worktree-arm is passed.
Exit 0 when every arm ran; 2 when two arms resolved to the same blob.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _git(*args: str) -> "str | None":
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def blob_at(rev: str, path: str) -> "str | None":
    return _git("rev-parse", f"{rev}:{path}")


def run_arm(test: str, label: str) -> "tuple[int, str]":
    # Fresh pycache per arm: timestamp-mode invalidation keys on (mtime, size),
    # and same-size arms written in one second reuse stale bytecode.
    with tempfile.TemporaryDirectory() as cache:
        env = {**os.environ, "PYTHONPYCACHEPREFIX": cache,
               "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, "-B", test], capture_output=True,
                           text=True, env=env)
    tail = [ln for ln in (r.stdout + r.stderr).splitlines()
            if ln.startswith(("OK", "FAILED", "Ran "))]
    # A silent test has no summary line; "" beats repeating rc, which the
    # caller already prints.
    return r.returncode, " | ".join(tail[-2:])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("test")
    p.add_argument("--file", required=True, help="the file swapped per arm")
    p.add_argument("--rev", action="append", default=[],
                   help="revision to arm against; repeatable, in order")
    p.add_argument("--no-worktree-arm", action="store_true")
    a = p.parse_args(argv)

    target = Path(a.file)
    if not target.is_file():
        print(f"arm-test: {a.file} is not a file", file=sys.stderr)
        return 1
    saved = target.read_bytes()
    worktree_blob = _git("hash-object", a.file)

    rows, seen = [], {}
    try:
        for rev in a.rev:
            blob = blob_at(rev, a.file)
            if blob is None:
                print(f"arm-test: {rev}:{a.file} does not resolve — arm SKIPPED",
                      file=sys.stderr)
                continue
            content = _git("cat-file", "-p", blob)
            if content is None:
                print(f"arm-test: could not read blob {blob[:9]} — arm SKIPPED",
                      file=sys.stderr)
                continue
            target.write_text(content + "\n" if not content.endswith("\n") else content)
            rc, summary = run_arm(a.test, rev)
            rows.append((rev, _git("rev-parse", rev) or "?", blob, rc, summary))
            seen.setdefault(blob, []).append(rev)
        # On a clean tree this arm's blob IS HEAD's, so adding it would trip
        # the dupe refusal on a comparison nobody asked for.
        if not a.no_worktree_arm and (worktree_blob or "?") not in seen:
            target.write_bytes(saved)
            rc, summary = run_arm(a.test, "worktree")
            rows.append(("worktree", "-", worktree_blob or "?", rc, summary))
            seen.setdefault(worktree_blob or "?", []).append("worktree")
        elif not a.no_worktree_arm:
            print(f"arm-test: tree arm skipped — its blob "
                  f"{(worktree_blob or '?')[:9]} is already an arm", file=sys.stderr)
    finally:
        target.write_bytes(saved)

    width = max((len(r[0]) for r in rows), default=4)
    for rev, commit, blob, rc, summary in rows:
        print(f"ARM {rev:<{width}}  commit={commit[:9]:<9} blob={(blob or '?')[:9]}  "
              f"rc={rc}{('  ' + summary) if summary else ''}")

    dupes = {b: revs for b, revs in seen.items() if len(revs) > 1}
    if dupes:
        for b, revs in dupes.items():
            print(f"arm-test: NOT A COMPARISON — {', '.join(revs)} resolved to the "
                  f"same blob {b[:9]}; identical results prove nothing", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
