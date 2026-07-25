#!/usr/bin/env python3
"""Unit tests for scripts/review-checks.py (the hardcoded-path scanner behind
review-checks.sh). Imports the module under coverage and exercises token_at /
allowed / main across the diff-parsing branches. The shell-level behaviour is
covered by tests/review-checks.test.sh; this pins the Python internals + gives
the diff-coverage gate real coverage of the new file."""
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODPATH = REPO / "scripts" / "review-checks.py"

# Module reads RC_FLAGS / RC_ALLOWS at import time — set them first.
os.environ["RC_FLAGS"] = "\n".join(["/Users/", "/home/", "~/.claude"])
os.environ["RC_ALLOWS"] = "\n".join(["/tmp/", "/nonexistent", "example.com"])

spec = importlib.util.spec_from_file_location("review_checks", MODPATH)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

passed = 0
failed = 0


def ok(name, cond):
    global passed, failed
    if cond:
        print("ok   " + name)
        passed += 1
    else:
        print("FAIL " + name)
        failed += 1


def scan(diff):
    """Run main() with `diff` fed on STDIN (the runner streams it there — #2281),
    return (exit, stdout)."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(diff)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rc.main()
    finally:
        sys.stdin = old_stdin
    return code, buf.getvalue()


# --- token_at ---------------------------------------------------------------
ok("token_at extracts a quoted path token",
   rc.token_at('x = "/Users/alice/app";', 5) == "/Users/alice/app")
ok("token_at stops at a space delimiter",
   rc.token_at("p /home/bob end", 2) == "/home/bob")
ok("token_at expands left AND right from a mid-token position",
   rc.token_at("a=/Users/alice/app b", 10) == "/Users/alice/app")

# --- allowed: both branches -------------------------------------------------
ok("allowed: path-allow matches at token start", rc.allowed("/tmp/scratch") is True)
ok("allowed: path-allow anchored (no substring mask)", rc.allowed("/Users/tmp/keep") is False)
ok("allowed: domain-allow matches as substring", rc.allowed("https://example.com/x") is True)
ok("allowed: unrelated token not allowed", rc.allowed("/Users/alice") is False)

# --- main(): diff-parsing branches ------------------------------------------
code, out = scan('+++ b/src/app.ts\n@@ -1,0 +5,1 @@\n+const p = "/Users/a/b";')
ok("positive: /Users flagged with file:line", code == 0 and "src/app.ts:5:" in out and "/Users/a/b" in out)

code, out = scan(
    '+++ b/src/app.ts\n'
    '@@ -10,2 +10,3 @@\n'
    ' existing_line_one = 1\n'
    ' existing_line_two = 2\n'
    '+path = "/Users/a/b"'
)
ok("context lines advance the reported new-file line",
   code == 0 and "src/app.ts:12:" in out and "/Users/a/b" in out)

code, out = scan('+++ b/src/c.py\n@@ -1,0 +1,2 @@\n+# note /Users/x\n+  // note /home/y')
ok("comment lines (# and //) skipped", out.strip() == "")

code, out = scan('+++ b/docs/x.md\n@@ -1,0 +1,1 @@\n+see /Users/x')
ok("docs (.md) file skipped", out.strip() == "")

code, out = scan('+++ b/tests/x.test.sh\n@@ -1,0 +1,1 @@\n+D="/Users/x"')
ok("tests/ file skipped", out.strip() == "")

code, out = scan('+++ b/scripts/review-checks.py\n@@ -1,0 +1,1 @@\n+x = "/Users/self"')
ok("self (review-checks.py) skipped", out.strip() == "")

code, out = scan('+++ b/src/x.ts\n@@ -1,0 +1,1 @@\n-const gone = "/home/removed"\n+const stay = ok()')
ok("removed (-) lines ignored; clean add passes", out.strip() == "")

code, out = scan('+++ b/g.sh\n@@ -1,0 +1,1 @@\n+cfg=~/.claude/settings.json')
ok("~/.claude flagged", "~/.claude" in out)

code, out = scan('+++ b/m.js\n@@ -1,0 +1,1 @@\n+x="/Users/a"; y="https://example.com"')
ok("mixed forbidden+allowed still flags real path", "/Users/a" in out)

code, out = scan("")
ok("empty diff -> no output, exit 0", code == 0 and out == "")

# --- oversized input can't bypass the scan (#2281) --------------------------
# A diff far larger than the OS argv/env limit (~1MB on macOS) used to be passed
# via the RC_DIFF env var and blew 'Argument list too long', silently skipping
# the scan. Streamed on stdin it must still flag the embedded hardcoded path.
big = ["+++ b/big.js", "@@ -1,0 +1,200001 @@"]
big += ["+const filler = resolveWorkspace();"] * 200000
big += ['+const home = "/Users/alice/secret";']
big_diff = "\n".join(big)
assert len(big_diff) > 1048576, "regression fixture must exceed ARG_MAX"
code, out = scan(big_diff)
ok("oversized diff (>ARG_MAX) still flags hardcoded path",
   code == 0 and "big.js:200001:" in out and "/Users/alice/secret" in out)

# --- docstring prose is not a hardcoded path (#2313 false-positive) ---------
# A PR that ADDS a docstring describing a legacy path it's removing must not be
# flagged for the path sitting in that prose.
code, out = scan(
    '+++ b/src/health-check.py\n'
    '@@ -1,0 +1,3 @@\n'
    '+    """Detect unlinked skills.\n'
    '+    On a migrated install this scanned a stale ~/.claude/skills/ path.\n'
    '+    """'
)
ok("path inside a multi-line docstring is NOT flagged (#2313)", out.strip() == "")

# But real code AFTER the docstring closes is still scanned.
code, out = scan(
    '+++ b/src/x.py\n'
    '@@ -1,0 +1,3 @@\n'
    '+    """doc line one\n'
    '+    doc line two."""\n'
    '+    skills = "/Users/real/path"'
)
ok("code after a docstring still flags a hardcoded path",
   "/Users/real/path" in out)

# A single-line pair of triple-quotes (open+close) leaves state unchanged, so a
# real path on the NEXT line is still caught (parity not corrupted).
code, out = scan(
    '+++ b/src/y.py\n'
    '@@ -1,0 +1,2 @@\n'
    '+    doc = """inline"""\n'
    '+    p = "/Users/z"'
)
ok("even triple-quote count doesn't corrupt state; next line flags", "/Users/z" in out)

print("---")
if failed:
    print("FAILED — %d of %d" % (failed, passed + failed))
    sys.exit(1)
print("PASS — review-checks.py internals (%d checks)" % passed)
sys.exit(0)
