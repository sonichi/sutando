#!/usr/bin/env python3
"""Regression for src/archive-stale-results.py's empty-file exclusion.

The archiver moves stale results/*.txt into a dated archive dir on mtime. An
EMPTY .txt must be excluded: a producer can create a proactive-*.txt and pause
before its first flush, and an empty file keeps its creation mtime while the
descriptor is open. Moving it on age would strand the producer's later flush in
the archived inode — the exact data-loss the proactive drain removes
(sonichi/sutando#2324). The archiver is the other mtime-keyed mover of these
files, so it must make the same exclusion for the guarantee to hold end-to-end.

Runs the archiver IN-PROCESS (import + main()) rather than as a subprocess, so
the diff-coverage gate instruments the new exclusion branch — a subprocess call
executes the lines but coverage on the parent process never sees them.

Guards:
  1. a CONTENTFUL stale .txt is archived (flood prevention still works)
  2. an EMPTY stale .txt is NOT archived — left in place for the drain
  3. a fresh (non-stale) .txt is untouched regardless of size

Run: python3 tests/archive-stale-results.test.py   (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def _load_archiver(workspace: Path):
    # The module resolves its workspace + reads RETENTION_HOURS/DRY_RUN at IMPORT
    # time, so the env must be set before exec_module. SUTANDO_TEST_MODE lets
    # resolve_workspace honor SUTANDO_WORKSPACE (post-#1440 it is otherwise
    # ignored). Import (not subprocess) so coverage instruments main().
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["RETENTION_HOURS"] = "24"
    os.environ.pop("DRY_RUN", None)
    spec = importlib.util.spec_from_file_location(
        "archiver_ut", REPO / "src" / "archive-stale-results.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="archiver-empty-"))
    # Pre-satisfy the in-repo migrators so resolve_workspace() at import doesn't
    # relocate this repo's notes/build_log into the throwaway workspace.
    (tmp / ".notes-migrated").touch()
    (tmp / ".build_log-migrated").touch()

    arch = _load_archiver(tmp)
    results = arch.RESULTS  # = <tmp>/results, captured at import
    results.mkdir(parents=True, exist_ok=True)

    old = time.time() - 48 * 3600  # well past the default 24h retention
    contentful = results / "proactive-contentful.txt"
    contentful.write_text("a real stale nudge body\n")
    os.utime(contentful, (old, old))

    empty = results / "proactive-empty.txt"
    empty.write_text("")  # 0 bytes — a producer that has not flushed yet
    os.utime(empty, (old, old))

    # air's #2360 follow-up: a whitespace-only file is NON-zero size but empty
    # after strip() — a producer that wrote a newline/header then paused. A
    # size-only check would archive it; the drain would not. The two movers must
    # agree, so this must ALSO be left in place.
    whitespace = results / "proactive-whitespace.txt"
    whitespace.write_text("   \n\t\n")  # size > 0, strip() == ""
    os.utime(whitespace, (old, old))

    # John's #2360 ask: an invalid-UTF-8 stale file exercises the fail-safe
    # OSError/UnicodeDecodeError branch (src/archive-stale-results.py:102-103). A
    # file we cannot decode is exactly one that may be mid-write, so read_text()
    # raising must leave it in place, never archive it on age.
    undecodable = results / "proactive-binary.txt"
    undecodable.write_bytes(b"\xff\xfe\x00\x80 partial write")  # invalid UTF-8
    os.utime(undecodable, (old, old))

    fresh_empty = results / "proactive-fresh.txt"
    fresh_empty.write_text("")  # 0 bytes but recent — also must stay

    rc = arch.main()  # in-process → coverage sees the strip-empty exclusion branch
    check("archiver main() returns 0", rc == 0, f"rc={rc}")

    archived_names = {p.name for p in results.glob("archive-*/*.txt")}

    check("contentful stale .txt is archived (flood prevention intact)",
          "proactive-contentful.txt" in archived_names and not contentful.exists(),
          f"archived={sorted(archived_names)}")
    check("empty stale .txt is NOT archived — left in place for the drain",
          empty.exists() and "proactive-empty.txt" not in archived_names,
          f"empty exists={empty.exists()} archived={sorted(archived_names)}")
    check("whitespace-only stale .txt is NOT archived (strip-empty, matches the drain)",
          whitespace.exists() and "proactive-whitespace.txt" not in archived_names,
          f"whitespace exists={whitespace.exists()} archived={sorted(archived_names)}")
    check("invalid-UTF-8 stale .txt is NOT archived (fail-safe branch: may be mid-write)",
          undecodable.exists() and "proactive-binary.txt" not in archived_names,
          f"undecodable exists={undecodable.exists()} archived={sorted(archived_names)}")
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
