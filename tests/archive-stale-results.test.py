#!/usr/bin/env python3
"""Regression for src/archive-stale-results.py's empty-file exclusion.

The archiver moves stale results/*.txt into a dated archive dir on mtime. An
EMPTY .txt must be excluded: a producer can create a proactive-*.txt and pause
before its first flush, and an empty file keeps its creation mtime while the
descriptor is open. Moving it on age would strand the producer's later flush in
the archived inode — the exact data-loss the proactive drain removes
(sonichi/sutando#2324). The archiver is the other mtime-keyed mover of these
files, so it must make the same exclusion for the guarantee to hold end-to-end.

Guards:
  1. a CONTENTFUL stale .txt is archived (flood prevention still works)
  2. an EMPTY stale .txt is NOT archived — left in place for the drain
  3. a fresh (non-stale) .txt is untouched regardless of size

Run: python3 tests/archive-stale-results.test.py   (exit 0/1)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "archive-stale-results.py"
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="archiver-empty-"))
    results = tmp / "results"
    results.mkdir()
    # Pre-satisfy the in-repo migrators so resolve_workspace() at import doesn't
    # relocate this repo's notes/build_log into the throwaway workspace.
    (tmp / ".notes-migrated").touch()
    (tmp / ".build_log-migrated").touch()

    old = time.time() - 48 * 3600  # well past the default 24h retention
    contentful = results / "proactive-contentful.txt"
    contentful.write_text("a real stale nudge body\n")
    os.utime(contentful, (old, old))

    empty = results / "proactive-empty.txt"
    empty.write_text("")  # 0 bytes — a producer that has not flushed yet
    os.utime(empty, (old, old))

    fresh_empty = results / "proactive-fresh.txt"
    fresh_empty.write_text("")  # 0 bytes but recent — also must stay

    env = dict(os.environ)
    env["SUTANDO_TEST_MODE"] = "1"
    env["SUTANDO_WORKSPACE"] = str(tmp)
    env.pop("DRY_RUN", None)
    env["RETENTION_HOURS"] = "24"
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout); print(r.stderr, file=sys.stderr)
        check("archiver exits 0", False, f"rc={r.returncode}")
        print(f"FAIL — {len(fails)}: {fails}")
        return 1

    archived = list(results.glob("archive-*/*.txt"))
    archived_names = {p.name for p in archived}

    check("contentful stale .txt is archived (flood prevention intact)",
          "proactive-contentful.txt" in archived_names and not contentful.exists(),
          f"archived={sorted(archived_names)}")
    check("empty stale .txt is NOT archived — left in place for the drain",
          empty.exists() and "proactive-empty.txt" not in archived_names,
          f"empty exists={empty.exists()} archived={sorted(archived_names)}")
    check("fresh empty .txt is untouched",
          fresh_empty.exists() and "proactive-fresh.txt" not in archived_names)

    print()
    if fails:
        print(f"FAIL — {len(fails)}: {fails}")
        return 1
    print("PASS — archiver excludes empty results files (open-fd invariant)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
