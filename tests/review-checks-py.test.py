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
os.environ["RC_FLAGS_EXACT"] = "\n".join(["/usr/bin/swift", "/usr/bin/make"])
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

# A multi-line ASSIGNED string (not a docstring) must NOT be exempted — a
# hardcoded path inside it is still flagged (#2281, Qingyun review). The old
# count-only toggle opened the exempt span on `COMMAND = """`, letting host
# config slip past this required gate under a false-green check.
code, out = scan(
    '+++ b/src/deploy.py\n'
    '@@ -1,0 +1,3 @@\n'
    '+COMMAND = """\n'
    '+rsync /Users/alice/data /backup\n'
    '+"""'
)
ok("multi-line assigned string does NOT exempt a hardcoded path (#2281 bypass)",
   "/Users/alice/data" in out)

# ...and a prefixed real docstring (r\"\"\") still exempts its prose.
code, out = scan(
    '+++ b/src/z.py\n'
    '@@ -1,0 +1,3 @@\n'
    '+    r"""Legacy note.\n'
    '+    Old path was /Users/legacy/thing.\n'
    '+    """'
)
ok("prefixed (r) docstring prose is still NOT flagged", out.strip() == "")

# --- JSDoc / block-comment continuation lines -------------------------------
# The `#` and `//` skips had no equivalent for ` * ` continuation lines, so
# prose inside a /** … */ block was scanned as code. That bites once the flag
# list carries tool paths, because the modules that RESOLVE those tools
# necessarily document the hazard in JSDoc.
code, out = scan(
    '+++ b/src/app.ts\n'
    '@@ -1,0 +1,4 @@\n'
    '+/**\n'
    '+ * Legacy note: the old path was /Users/legacy/thing.\n'
    '+ *\n'
    '+ */'
)
ok("JSDoc continuation prose is NOT flagged", out.strip() == "")

# The two shapes that must STAY scannable, so the skip cannot be used as a
# bypass: a `/* … */ code` one-liner, and a generator method (`*name()`, no
# space after the star).
code, out = scan(
    '+++ b/src/app.ts\n'
    '@@ -1,0 +1,1 @@\n'
    '+/* inline */ const sneaky = "/Users/a/b";'
)
ok("an inline /* */ comment does not exempt code on the same line",
   "src/app.ts:1:" in out)

code, out = scan(
    '+++ b/src/app.ts\n'
    '@@ -1,0 +1,1 @@\n'
    '+  *gen() { return "/Users/a/b"; }'
)
ok("a generator method is not mistaken for a comment", "src/app.ts:1:" in out)

# --- block-comment suppression must be STATEFUL (#2474 review, bypass) ------
# `* ` at the start of a line is not proof of a comment: it is also a continued
# multiplication. Suppressing on that shape alone let a real path through.
code, out = scan(
    '+++ b/src/x.js\n'
    '@@ -1,0 +1,2 @@\n'
    '+const n = 2\n'
    '+  * "/Users/alice/secret".length;'
)
ok("continued multiplication is NOT mistaken for JSDoc (scanner bypass)",
   "/Users/alice/secret" in out)

# Genuine JSDoc prose stays exempt...
code, out = scan(
    '+++ b/src/x.ts\n'
    '@@ -1,0 +1,4 @@\n'
    '+/**\n'
    '+ * Legacy note: the old path was /Users/legacy/thing.\n'
    '+ *\n'
    '+ */'
)
ok("JSDoc body inside a real /* */ span is exempt", out.strip() == "")

# ...and code AFTER the block closes is scanned again.
code, out = scan(
    '+++ b/src/x.ts\n'
    '@@ -1,0 +1,3 @@\n'
    '+/** note */\n'
    '+const p = "/Users/after/block";'
)
ok("code after a closed block comment is scanned", "/Users/after/block" in out)

# --- JSDoc body whose opener is OUTSIDE the hunk (#2474 review) -------------
# A unified diff carries 3 context lines, so editing a JSDoc body more than 3
# lines below its `/**` leaves the opener out of the hunk. Block state must be
# inferred from the hunk's own content, not reset to False.
code, out = scan(
    '+++ b/src/x.js\n'
    '@@ -20,3 +20,4 @@\n'
    ' * earlier prose\n'
    '+ * On macOS /Users/legacy/thing is gone.\n'
    ' */\n'
    ' export function x() {}'
)
ok("JSDoc body is exempt when its opener is outside the hunk", out.strip() == "")

# The path sits in the comment portion of the line that OPENS the comment, so a
# line-level `state before this line` test gets it wrong.
code, out = scan(
    '+++ b/src/y.js\n'
    '@@ -1,0 +1,2 @@\n'
    '+/** helper for /Users/legacy/thing resolution\n'
    '+ *  more prose'
)
ok("path inside the comment portion of an opening line is exempt", out.strip() == "")

# ...but code AFTER a close on the same line is still scanned.
code, out = scan(
    '+++ b/src/z.js\n'
    '@@ -1,0 +1,1 @@\n'
    '+/* note */ const p = "/Users/after/close";'
)
ok("code after a same-line comment close is scanned", "/Users/after/close" in out)

# The inference must not swallow the bypass: no delimiters in the hunk at all
# means "assume code".
code, out = scan(
    '+++ b/src/m.js\n'
    '@@ -1,0 +1,2 @@\n'
    '+const n = 2\n'
    '+  * "/Users/alice/secret".length;'
)
ok("hunk inference does not resurrect the multiplication bypass",
   "/Users/alice/secret" in out)

# --- a quoted "*/" must not establish block state (#2474 review, bypass) ----
# The hunk-start inference treated the first `*/` anywhere as proof the hunk
# began inside a comment, so a string literal containing it masked the
# executable code before it.
code, out = scan(
    '+++ b/src/x.js\n'
    '@@ -1,0 +1,1 @@\n'
    '+const p = "/Users/alice/secret"; const closer = "*/";'
)
ok("a quoted */ does not establish block state (scanner bypass)",
   "/Users/alice/secret" in out)

# _blank_string_literals directly: escapes and each quote style.
ok("blank: double-quoted span is blanked",
   rc._blank_string_literals('a = "*/" ; b') == 'a =      ; b')
ok("blank: single-quoted span is blanked",
   "*/" not in rc._blank_string_literals("a = '*/'"))
ok("blank: template literal is blanked",
   "*/" not in rc._blank_string_literals("a = `*/`"))
ok("blank: escaped quote does not end the span early",
   "*/" not in rc._blank_string_literals('a = "x\\"*/" ; end'))
ok("blank: code outside strings is preserved",
   rc._blank_string_literals("const x = 1;") == "const x = 1;")

# An UNquoted mid-line */ still does not establish block state — only a
# line-start closer does, so a stray token cannot suppress the rest of a hunk.
code, out = scan(
    '+++ b/src/x.js\n'
    '@@ -1,0 +1,1 @@\n'
    '+foo(); /* note */ const p = "/Users/mid/line";'
)
ok("mid-line comment close does not suppress later code", "/Users/mid/line" in out)

# --- a line-start */ hidden in a MULTI-LINE template literal (#2474 review) --
# _blank_string_literals is single-line, so it loses the opening backtick before
# the next line is examined. A closer therefore only counts when an earlier line
# in the hunk already looks like comment body.
code, out = scan(
    '+++ b/src/x.js\n'
    '@@ -1,0 +1,3 @@\n'
    '+const p = "/Users/alice/secret"; const tpl = `\n'
    '+*/\n'
    '+`;'
)
ok("a */ inside a multi-line template does not establish block state",
   "/Users/alice/secret" in out)

# --- flag_exact: whole-token, not substring (#2474 review) ------------------
# A full executable path must not reject longer siblings in the same directory
# family: /usr/bin/swift-inspect is a separate REAL binary (own inode, link
# count 1) while /usr/bin/swift is the stub.
code, out = scan('+++ b/src/a.swift\n@@ -1,0 +1,1 @@\n+p = "/usr/bin/swift"')
ok("flag_exact matches the exact token", "/usr/bin/swift" in out)

code, out = scan('+++ b/src/a.swift\n@@ -1,0 +1,1 @@\n+p = "/usr/bin/swift-inspect"')
ok("flag_exact does NOT match a longer sibling (swift-inspect)", out.strip() == "")

code, out = scan('+++ b/src/a.sh\n@@ -1,0 +1,1 @@\n+p = "/usr/bin/makeinfo"')
ok("flag_exact does NOT match a longer sibling (makeinfo)", out.strip() == "")

# `flag` keeps its substring semantics — /Users/ must still match a longer path.
code, out = scan('+++ b/src/a.py\n@@ -1,0 +1,1 @@\n+p = "/Users/alice/deep/path"')
ok("flag (substring) semantics unchanged alongside flag_exact",
   "/Users/alice/deep/path" in out)

print("---")
if failed:
    print("FAILED — %d of %d" % (failed, passed + failed))
    sys.exit(1)
print("PASS — review-checks.py internals (%d checks)" % passed)
sys.exit(0)
