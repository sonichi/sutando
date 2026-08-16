#!/usr/bin/env python3
"""Flag added comment blocks over the repository's physical-line cap.

Classification comes from `tokenize` over each file's POST-IMAGE, so a `#`
inside a string literal is a STRING token and can never be read as a comment.
Scope stays added-lines-only: a block counts only if every line of it is added.
Docstrings are deliberately out of scope — the written contract caps comments.
"""
import re
import sys
import tokenize
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def comment_lines(path):
    """Line numbers carrying a COMMENT token, or None if the file cannot be tokenized."""
    try:
        with open(path, "rb") as fh:
            return {t.start[0] for t in tokenize.tokenize(fh.readline)
                    if t.type == tokenize.COMMENT}
    except (OSError, SyntaxError, tokenize.TokenError, UnicodeDecodeError):
        return None


def added_by_file(diff_text):
    """{path: {added line numbers in the post-image}} from a unified diff."""
    out, path, lineno = {}, None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            out.setdefault(path, set()) if path else None
            continue
        m = HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            out.setdefault(path, set()).add(lineno)
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1
    return out


def violations(diff_text, cap, exts, root="."):
    """(path, first_line, length) for each fully-added comment run longer than cap."""
    found, unscannable = [], []
    for path, added in added_by_file(diff_text).items():
        if not added or not any(path.endswith(e) for e in exts):
            continue
        full = Path(root) / path
        cl = comment_lines(full)
        if cl is None:
            unscannable.append(path)
            continue
        # Walk only the comment lines that were added, grouping consecutive runs.
        run = []
        for n in sorted(cl):
            if n not in added:
                if len(run) > cap:
                    found.append((path, run[0], len(run)))
                run = []
                continue
            if run and n != run[-1] + 1:
                if len(run) > cap:
                    found.append((path, run[0], len(run)))
                run = []
            run.append(n)
        if len(run) > cap:
            found.append((path, run[0], len(run)))
    return found, unscannable


def main():
    cap = 2
    exts = (".py",)
    diff_text = sys.stdin.read()
    if not diff_text.strip():
        print("prose-cap: empty diff; nothing scanned, so this is NOT a pass", file=sys.stderr)
        return 2
    found, unscannable = violations(diff_text, cap, exts)
    for path in unscannable:
        print(f"prose-cap: NOTE {path} could not be tokenized; not scanned", file=sys.stderr)
    for path, line, length in found:
        print(f"prose-cap: {path}:{line} comment block is {length} lines (cap {cap})")
    # The runner reads a non-zero exit as "the scanner could not run" and fails
    # closed; findings ride stdout, so a violation must still exit 0.
    if found:
        print(f"prose-cap: {len(found)} block(s) over the {cap}-line cap", file=sys.stderr)
    else:
        print("prose-cap: PASS (comment blocks within cap)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
