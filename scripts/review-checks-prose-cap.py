#!/usr/bin/env python3
"""Flag added comment blocks over the repository's physical-line cap.

Classification comes from `tokenize` over each file's POST-IMAGE, so a `#`
inside a string literal is a STRING token and can never be read as a comment.
Scope stays added-lines-only: a block counts only if every line of it is added.
Docstrings are deliberately out of scope — the written contract caps comments.
"""
import hashlib
import os
import re
import sys
import tokenize
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
INDEX = re.compile(r"^index [0-9a-f]+\.\.([0-9a-f]+)")
# Fallbacks only. review-checks.sh normally supplies both from REVIEW.md.
DEFAULT_CAP = 2
DEFAULT_EXTS = (".py",)
# Classification is `tokenize`, so only Python comment syntax is decidable here.
# Any other extension yields zero COMMENT tokens — a clean PASS over unread text.
SUPPORTED_EXTS = (".py", ".pyi")


def comment_lines(path):
    """Line numbers carrying a COMMENT token, or None if the file cannot be tokenized."""
    try:
        with open(path, "rb") as fh:
            return {t.start[0] for t in tokenize.tokenize(fh.readline)
                    if t.type == tokenize.COMMENT}
    except (OSError, SyntaxError, tokenize.TokenError, UnicodeDecodeError):
        return None


def added_by_file(diff_text):
    """({path: {post-image line: text}}, {path: post-image blob sha}) from a diff.
    The sha rides `index a..b`; it is the only whole-file identity a diff carries."""
    out, shas, path, lineno, pending = {}, {}, None, 0, None
    for raw in diff_text.splitlines():
        m = INDEX.match(raw)
        if m:
            pending = m.group(1)
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            if path:
                out.setdefault(path, {})
                if pending:
                    shas[path] = pending
            pending = None
            continue
        m = HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if path is None or raw.startswith("\\"):
            continue  # "\ No newline at end of file" occupies no post-image line.
        if raw.startswith("+"):
            out.setdefault(path, {})[lineno] = raw[1:]
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1
    return out, shas


def post_image_matches(path, sha):
    """True only when the file hashes to the diff's post-image blob. Comparing the
    added lines is not enough: context outside the hunk decides how they tokenize."""
    if not sha:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    full = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return full.startswith(sha) or sha.startswith(full)


def violations(diff_text, cap, exts, root="."):
    """(found, unreadable, detached, in_scope). found = (path, first_line, length)
    per run over cap; in_scope counts ext-matching files with added lines, so the
    caller can tell "scanned clean" from "nothing was ever in scope"."""
    found, unreadable, detached = [], [], []
    in_scope = 0
    by_file, shas = added_by_file(diff_text)
    for path, added in by_file.items():
        if not added or not any(path.endswith(e) for e in exts):
            continue
        in_scope += 1
        full = Path(root) / path
        # A diff can legitimately arrive without its tree (`gh pr diff > pr.diff`),
        # and a tree at another revision is worse: its line numbers still resolve.
        if not full.exists() or not post_image_matches(full, shas.get(path)):
            detached.append(path)
            continue
        cl = comment_lines(full)
        if cl is None:
            unreadable.append(path)
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
    return found, unreadable, detached, in_scope


def _config():
    """(cap, exts) from the runner's env. review-checks.sh sources both from
    REVIEW.md's `checks:` block, so ignoring them makes that surface a lie."""
    raw_cap = os.environ.get("RC_PROSE_CAP", "").strip()
    try:
        cap = int(raw_cap) if raw_cap else DEFAULT_CAP
    except ValueError:
        cap = DEFAULT_CAP
    raw_exts = os.environ.get("RC_PROSE_EXTS", "").strip()
    exts = tuple(e.strip() for e in raw_exts.split(",") if e.strip()) or DEFAULT_EXTS
    unsupported = tuple(e for e in exts if e not in SUPPORTED_EXTS)
    return cap, exts, unsupported


def main():
    cap, exts, unsupported = _config()
    # Refuse rather than scan: an unsupported extension is selected, tokenized as
    # Python, found to hold no comments, and reported PASS without being read.
    if unsupported:
        for e in unsupported:
            print(f"prose-cap: configured extension {e!r} is not Python-tokenizable; "
                  "its comment syntax cannot be classified here", file=sys.stderr)
        print(f"prose-cap: FAIL-CLOSED — {len(unsupported)} configured extension(s) "
              f"unsupported (supported: {','.join(SUPPORTED_EXTS)})", file=sys.stderr)
        return 2
    diff_text = sys.stdin.read()
    if not diff_text.strip():
        print("prose-cap: empty diff; nothing scanned, so this is NOT a pass", file=sys.stderr)
        return 2
    found, unreadable, detached, in_scope = violations(diff_text, cap, exts)
    for path, line, length in found:
        print(f"prose-cap: {path}:{line} comment block is {length} lines (cap {cap})")
    # A file that IS here and still would not parse is a fault: it was meant to be
    # read and was not, so a verdict about it would be about a read that never ran.
    if unreadable:
        for path in unreadable:
            print(f"prose-cap: {path} has no readable post-image; NOT scanned", file=sys.stderr)
        print(f"prose-cap: FAIL-CLOSED — {len(unreadable)} in-scope file(s) unverified",
              file=sys.stderr)
        return 2
    # Findings ride stdout and must still exit 0: a violation is a successful scan.
    if found:
        print(f"prose-cap: {len(found)} block(s) over the {cap}-line cap", file=sys.stderr)
    elif detached:
        # Never a bare PASS here — the scan had nothing to read, which is a different
        # answer from "read it and it was clean", and the runner reprints the distinction.
        for path in detached:
            print(f"prose-cap: {path} has no matching post-image here; NOT scanned", file=sys.stderr)
        print(f"prose-cap: SKIPPED — {len(detached)} in-scope file(s) are not this tree's revision",
              file=sys.stderr)
    elif in_scope == 0:
        # A gate with nothing in scope cannot fail; PASS would read as "checked".
        print(f"prose-cap: no in-scope files (exts {','.join(exts)}); nothing "
              "was scanned", file=sys.stderr)
    else:
        print(f"prose-cap: PASS (comment blocks within cap {cap}, exts {','.join(exts)})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
