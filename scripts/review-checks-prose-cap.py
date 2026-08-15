#!/usr/bin/env python3
"""Flag added prose blocks over the repository's physical-line cap. Scans ADDED
lines only. Exit non-zero = cannot scan (the runner fails closed on that)."""
import os
import re
import sys

DIFF_GIT = re.compile(r"^diff --git ")
PLUS_B = re.compile(r"^\+\+\+ (.+)$")
HUNK = re.compile(r"^@@ .* \+(\d+)")
# A docstring OPENS a line; triple quotes mid-line are a string literal
# (this scanner's own pattern was the first false positive it caught).
OPENS = re.compile(r'^[ \t]*[rRbBuUfF]{0,2}("""|\'\'\')')


def _unquote(p):
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    try:
        return p[1:-1].encode("latin-1").decode("unicode_escape") \
                      .encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return p[1:-1]


def _blocks(added):
    """Prose runs in a list of (lineno, text) ADDED lines: comment runs, and
    triple-quoted blocks whose opening AND closing quotes are both added."""
    out, i = [], 0
    while i < len(added):
        no, txt = added[i]
        s = txt.strip()
        if s.startswith("#"):
            j = i
            while j + 1 < len(added) and added[j + 1][1].strip().startswith("#") \
                    and added[j + 1][0] == added[j][0] + 1:
                j += 1
            out.append(("comment", no, j - i + 1))
            i = j + 1
            continue
        q = OPENS.findall(txt)
        if q:
            if txt.strip().count(q[0]) >= 2:      # opens and closes on one line
                i += 1
                continue
            j = i + 1
            while j < len(added) and added[j][0] == added[j - 1][0] + 1:
                if q[0] in added[j][1]:
                    out.append(("docstring", no, j - i + 1))
                    break
                j += 1
            else:
                j = i                             # never closed inside the diff
            i = j + 1
            continue
        i += 1
    return out


def violations(diff_text, cap, exts):
    hits, path, added, lineno = [], None, [], 0

    def flush():
        if not path or not added:
            return
        if not any(path.endswith(e) for e in exts):
            return
        for kind, no, span in _blocks(added):
            if span > cap:
                hits.append(f"{path}:{no}: {kind} block is {span} physical lines (cap {cap})")

    for raw in diff_text.splitlines():
        if DIFF_GIT.match(raw):
            flush(); path, added = None, []
            continue
        m = PLUS_B.match(raw)
        if m:
            flush()
            p = _unquote(m.group(1).strip())
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            added = []
            continue
        h = HUNK.match(raw)
        if h:
            lineno = int(h.group(1)); continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append((lineno, raw[1:])); lineno += 1
        elif not raw.startswith("-"):
            lineno += 1
    flush()
    return hits


def main():
    cap = int(os.environ.get("RC_PROSE_CAP") or 2)
    exts = [e for e in (os.environ.get("RC_PROSE_EXTS") or ".py").split(",") if e]
    for h in violations(sys.stdin.read(), cap, exts):
        print(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
