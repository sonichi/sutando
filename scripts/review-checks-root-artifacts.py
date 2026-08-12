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
the caller decides pass/fail from the output — and non-zero if this scanner
cannot do its job, so the runner fails closed. An empty glob list counts as
cannot: a scan that ran no patterns has not established anything.
"""
import fnmatch
import os
import re
import sys

NEW_FILE = re.compile(r"^new file mode ")
# Captures the raw token: when git quotes a path the quotes wrap the `b/` too
# (`+++ "b/pr\303\251body.md"`), so anchoring on `b/` would not match at all.
PLUS_B = re.compile(r"^\+\+\+ (.+)$")
RENAME_TO = re.compile(r"^rename to (.+)$")
# Only used to reset per-file state, so it must match the quoted form too
# (`diff --git "a/x" "b/y"`); capturing the path here would not.
DIFF_GIT = re.compile(r"^diff --git ")


def _unquote(p):
    """Git quotes a path containing non-ASCII or control bytes and C-escapes it.
    Left encoded, such a path matches no glob and slips the gate silently."""
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    try:
        return p[1:-1].encode("latin-1").decode("unicode_escape") \
                      .encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return p[1:-1]


def violations(diff_text, globs):
    """Paths ARRIVING at the repo root that match a flagged glob.

    Arrival, not authorship: a rename lands an artifact at the root just as an
    add does, and in a base..head diff a detected rename means the file was
    already committed — so `is_new` alone would miss it.
    """
    hits = []
    is_new = False

    def consider(p):
        if "/" in p:            # not at the root — out of scope, deliberately
            return
        for g in globs:
            if fnmatch.fnmatchcase(p, g):
                hits.append((p, g))
                return

    for line in diff_text.splitlines():
        # Resetting per file is load-bearing: without it the file after an
        # addition inherits is_new and a modification reads as an addition.
        if DIFF_GIT.match(line):
            is_new = False
            continue
        if NEW_FILE.match(line):
            is_new = True
            continue
        m = RENAME_TO.match(line)
        if m:
            # The only line naming the destination; covers pure renames and
            # rename+modify, neither of which carries `new file mode`.
            consider(_unquote(m.group(1)))
            continue
        m = PLUS_B.match(line)
        if not m:
            continue
        raw = m.group(1)
        # /dev/null means a deletion; only arrivals can strand an artifact.
        if raw == "/dev/null" or not is_new:
            continue
        p = _unquote(raw)
        if not p.startswith("b/"):
            continue
        consider(p[2:])
    return hits


def main():
    # Strip before the emptiness test, so a whitespace-only value is the config
    # error it is rather than one glob that matches nothing — matching how the
    # shell runner decides whether to install its defaults.
    raw = os.environ.get("RC_ROOT_ARTIFACT_GLOBS", "").split("\n")
    globs = [g for g in (s.strip() for s in raw) if g]
    if not globs:
        # An unconfigured scan has checked nothing. Returning 0 would let the
        # caller print "root-artifacts clean" — the shape of green check this
        # whole gate exists to prevent. The runner reads non-zero as fail-closed.
        print("review-checks-root-artifacts: RC_ROOT_ARTIFACT_GLOBS is empty; "
              "refusing to report a clean tree from a scan that ran no patterns.",
              file=sys.stderr)
        return 2
    for p, g in violations(sys.stdin.read(), globs):
        print("%s: PR-draft artifact added at the repo root (matches '%s')" % (p, g))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
