#!/usr/bin/env python3
"""Hardcoded-path scanner for review-checks.sh (kept as a sibling file so the
shell runner never embeds a heredoc inside $(), which macOS's bash 3.2
mis-parses). Inputs via env: RC_FLAGS / RC_ALLOWS (newline-separated pattern
lists from the guide's checks: block) and RC_DIFF (the unified diff). Prints one
`file:line: hardcoded path (tok): text` per violation to stdout; exit is always
0 — the caller decides pass/fail from whether anything was printed."""
import os
import re
import sys

flags = [p for p in os.environ.get("RC_FLAGS", "").split("\n") if p]
allows = [a for a in os.environ.get("RC_ALLOWS", "").split("\n") if a]
DELIMS = set("\"'()" + ", ;=" + chr(96) + chr(9))   # quotes, brackets, backtick, tab, etc.
SKIP = re.compile(r"\.md$|(^|/)tests/|\.test\.|review-checks\.(sh|py)$")


def token_at(s, pos):
    """The path-ish token containing index `pos` — expand to nearest delimiters."""
    left = pos
    while left > 0 and s[left - 1] not in DELIMS:
        left -= 1
    right = pos
    while right < len(s) - 1 and s[right + 1] not in DELIMS:
        right += 1
    return s[left:right + 1]


def allowed(tok):
    """A path-like allow (starts with / or ~) must match at the token START, so
    a fixture like `/tmp/` cannot mask a real `/Users/tmp/...`; a non-path allow
    (e.g. a domain) matches anywhere in the token."""
    for a in allows:
        if a[:1] in ("/", "~"):
            if tok.startswith(a):
                return True
        elif a in tok:
            return True
    return False


def main():
    skip = False
    ln = 0
    cur_file = ""
    hits = 0
    for raw in os.environ.get("RC_DIFF", "").split("\n"):
        if raw.startswith("+++ "):
            f = raw[4:].split("\t")[0]
            if f.startswith("b/"):
                f = f[2:]
            cur_file = f
            ln = 0
            skip = bool(SKIP.search(f))
            continue
        if raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                ln = int(m.group(1))
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            if skip:
                continue
            line = raw[1:]
            cur = ln
            ln += 1
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for p in flags:
                pos = line.find(p)
                if pos < 0:
                    continue
                tok = token_at(line, pos)
                if not allowed(tok):
                    print("%s:%d: hardcoded path (%s): %s" % (cur_file, cur, tok, stripped))
                    hits += 1
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
