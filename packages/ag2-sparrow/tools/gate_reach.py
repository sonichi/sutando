#!/usr/bin/env python3
"""Does a set of changed repo paths reach the ag2-sparrow publish gate?

A path reaches it when it is a src/ file synced into the package (sync_from_src.MAP) or matches
a glob in gate_inputs.txt. CI's classifier pipes the PR's changed paths in and reads true/false;
tests/sparrow-gate-inputs-cover-package-loads.test.py fails when a package test loads a repo file
outside the package that neither covers.

    git diff --name-only BASE...HEAD | python3 packages/ag2-sparrow/tools/gate_reach.py
"""
import fnmatch
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from sync_from_src import MAP  # noqa: E402

INPUTS = TOOLS / "gate_inputs.txt"


def globs() -> list:
    return [ln.strip() for ln in INPUTS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def covers(path: str, patterns=None) -> bool:
    """fnmatch semantics: `*` also crosses `/`, so `tests/_helpers/*` covers the whole tree."""
    if path in MAP:
        return True
    return any(fnmatch.fnmatchcase(path, g) for g in (globs() if patterns is None else patterns))


def reaches(paths) -> bool:
    patterns = globs()
    return any(covers(p, patterns) for p in paths if p)


if __name__ == "__main__":
    print("true" if reaches(sys.stdin.read().splitlines()) else "false")
