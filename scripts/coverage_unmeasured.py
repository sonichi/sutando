#!/usr/bin/env python3
"""Changed Python files that coverage never measured at all.

`diff-cover` reports only on files PRESENT in coverage.xml, so a file outside
`[run] source` is neither covered nor uncovered — it is invisible, and the gate
passes without having looked at it. Naming those files is what separates "not
measured" from "measured and clean".

Usage: coverage_unmeasured.py <base-ref> <coverage.json> [--rcfile .coveragerc]
Prints one repo-relative path per line; empty output means nothing was missed.
"""
from __future__ import annotations

import configparser
import fnmatch
import json
import subprocess
import sys
from pathlib import Path


def omit_globs(rcfile: str | Path) -> list[str]:
    """The `[run] omit` patterns, read from coverage's own config.

    Read rather than restated: a hardcoded copy drifts the moment someone edits
    .coveragerc, and drifts silently in the direction of false alarms.
    """
    cp = configparser.ConfigParser()
    try:
        cp.read(rcfile)
    except (configparser.Error, OSError):
        return []
    raw = cp.get("run", "omit", fallback="")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def is_omitted(path: str, globs: "list[str]") -> bool:
    """Coverage matches an omit pattern against the path as a whole."""
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, g)
               for g in globs)


def unmeasured(changed: "list[str]", measured: "set[str]",
               globs: "list[str]") -> "list[str]":
    """Changed files that are neither omitted on purpose nor present in the report.

    `measured` holds coverage.json's repository-relative file identities.
    XML filenames can lose the source root and alias unrelated files.
    """
    out = []
    for path in changed:
        if is_omitted(path, globs):
            continue
        if path in measured:
            continue
        out.append(path)
    return out


def measured_files(report_path: "str | Path") -> "set[str]":
    try:
        report = json.loads(Path(report_path).read_text())
    except (ValueError, OSError):
        return set()
    files = report.get("files") if isinstance(report, dict) else None
    return {Path(name).as_posix() for name in files} if isinstance(files, dict) else set()


def changed_py(base: str) -> "list[str]":
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD", "--", "*.py"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def main(argv: "list[str]") -> int:
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    base, report_path = argv[1], argv[2]
    rcfile = argv[4] if len(argv) > 4 and argv[3] == "--rcfile" else ".coveragerc"
    for path in unmeasured(changed_py(base), measured_files(report_path), omit_globs(rcfile)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
