#!/usr/bin/env python3
"""Flag PR-draft artifacts committed to the REPO ROOT, for review-checks.sh.

A sibling of review-checks.py rather than part of it: that scanner reasons about
added LINE CONTENT, this one about added FILE PATHS, and a stray root file never
appears as an added line at all — only as a diff header. Globs arrive via env
(RC_ROOT_ARTIFACT_GLOBS); the diff is read from STDIN, never argv, so a large PR
diff cannot hit 'Argument list too long' and make the scan silently skip.

Root-only by design: `tests/` and `skills/` legitimately carry .md and .patch
fixtures, and a rule reaching into them is one a maintainer disables the first
time it blocks a real fixture.

Prints one `path: <reason>` per violation. Exit 0 whether or not there are hits —
the caller decides pass/fail from the output — and non-zero only if this scanner
itself fails, so the runner can fail closed.
"""
import fnmatch
import os
import re
import sys

NEW_FILE = re.compile(r"^new file mode ")
PLUS_B = re.compile(r"^\+\+\+ b/(.+)$")
DIFF_GIT = re.compile(r"^diff --git a/(?:.+) b/(.+)$")


def violations(diff_text, globs):
    """Added files whose path is at the repo root and matches a flagged glob."""
    hits = []
    is_new = False
    for line in diff_text.splitlines():
        # Resetting per file is load-bearing: without it the file after an
        # addition inherits is_new and a modification reads as an addition.
        if DIFF_GIT.match(line):
            is_new = False
            continue
        if NEW_FILE.match(line):
            is_new = True
            continue
        m = PLUS_B.match(line)
        if not m:
            continue
        p = m.group(1)
        # /dev/null means a deletion; only additions can strand an artifact.
        if p == "/dev/null" or not is_new:
            continue
        if "/" in p:            # not at the root — out of scope, deliberately
            continue
        for g in globs:
            if fnmatch.fnmatchcase(p, g):
                hits.append((p, g))
                break
    return hits


def main():
    globs = [g for g in os.environ.get("RC_ROOT_ARTIFACT_GLOBS", "").split("\n") if g]
    if not globs:
        return 0
    for p, g in violations(sys.stdin.read(), globs):
        print("%s: PR-draft artifact added at the repo root (matches '%s')" % (p, g))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
