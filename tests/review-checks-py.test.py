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
# Contextual (paired) allow — exercised below. Parsed at import like the others.
os.environ["RC_ALLOW_PAIRED"] = "/opt/homebrew/ :: /usr/local/"

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

# --- paired (contextual) allow: portable candidate list vs naked literal ------
# Unit-level, so the diff-coverage gate sees these lines: the shell suite drives
# the same logic through a SUBPROCESS, which `coverage run` does not instrument
# (.coveragerc sets parallel but no COVERAGE_PROCESS_START).
ok("paired parsed from env at import",
   ("/opt/homebrew/", "/usr/local/") in rc.paired)
ok("_tokens splits a candidate list into whole paths",
   "/usr/local/bin/ffmpeg" in rc._tokens('X = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")'))
# A line ending WITHOUT a trailing delimiter must still yield its last token —
# every other fixture here ends in a quote or paren, so this is the only case
# that reaches the flush-the-remainder branch.
ok("_tokens keeps a trailing token with no closing delimiter",
   rc._tokens("BIN=/usr/local/bin/ffmpeg")[-1] == "/usr/local/bin/ffmpeg")
# A bare `( ... )` is a tuple in Python and the COMMA OPERATOR in JS/TS, so the
# path decides. See `_TUPLE_LANG_SUFFIXES`.
ok("paired: same-basename companion exempts (python tuple)",
   rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                     'X = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg")',
                     None, "src/x.py"))
ok("paired: naked literal is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg", 'X = "/opt/homebrew/bin/ffmpeg"'))
ok("paired: coincidental companion of a DIFFERENT name is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'A = "/opt/homebrew/bin/ffmpeg"; B = "/usr/local/share/doc"'))
ok("paired: mismatched basename is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'A = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffprobe")'))

# --- a companion promised in a COMMENT is not a fallback ----------------------
# The pairing rule asks "does this line ALSO run the companion?" — only code can
# answer. Scanning the raw line let a comment satisfy it, so a naked
# Apple-Silicon-only literal passed the gate with `/usr/local` merely mentioned
# in prose. That is the exact blind spot the rule exists to close.
ok("_code_part strips a // comment",
   rc._code_part('X = "/opt/homebrew/bin/ffmpeg"; // /usr/local/bin/ffmpeg')
   == 'X = "/opt/homebrew/bin/ffmpeg"; ')
ok("_code_part strips a # comment",
   rc._code_part('X = "/opt/homebrew/bin/ffmpeg"  # /usr/local/bin/ffmpeg')
   == 'X = "/opt/homebrew/bin/ffmpeg"  ')
# Must NOT truncate on a marker inside a string literal, or a URL would sever
# the line and silently drop real code from the companion search.
ok("_code_part keeps // inside a string literal (URL)",
   rc._code_part('U = "https://x.example/a"; V = "/usr/local/bin/ffmpeg"')
   == 'U = "https://x.example/a"; V = "/usr/local/bin/ffmpeg"')
ok("_code_part keeps # inside a string literal (fragment)",
   rc._code_part('U = "https://x.example/a#frag"')
   == 'U = "https://x.example/a#frag"')
ok("paired: companion only in a // comment is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'const F = "/opt/homebrew/bin/ffmpeg"; // fallback: /usr/local/bin/ffmpeg'))
ok("paired: companion only in a # comment is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'F = "/opt/homebrew/bin/ffmpeg"  # fallback /usr/local/bin/ffmpeg'))
# The over-narrowing control: the real candidate list must STILL be exempt.
# Without this, "stop honouring the pairing at all" would pass every case above.
ok("paired: a real candidate list is still exempt after comment-stripping",
   rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                     "FFMPEG = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg', 'ffmpeg']"))
ok("paired: candidate list with a trailing comment is still exempt",
   rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                     "F = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg']  # portable"))

# Handling only `#` and `//` left the identical bypass open in a THIRD syntax:
# a companion promised inside `/* … */` is no more executable than one after `//`.
ok("_code_part strips a /* block comment",
   rc._code_part('X = "/opt/homebrew/bin/ffmpeg"; /* /usr/local/bin/ffmpeg */')
   == 'X = "/opt/homebrew/bin/ffmpeg"; ')
ok("paired: companion only in a /* */ comment is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'const F = "/opt/homebrew/bin/ffmpeg"; /* fallback: /usr/local/bin/ffmpeg */'))
ok("_code_part keeps /* inside a string literal",
   rc._code_part('U = "a/*b"; V = "/usr/local/bin/ffmpeg"')
   == 'U = "a/*b"; V = "/usr/local/bin/ffmpeg"')
# Over-narrowing control for this syntax too: a real candidate list followed by
# a block comment must still qualify — the companion is in the CODE before it.
ok("paired: candidate list followed by a /* */ comment is still exempt",
   rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                     "F = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg']  /* portable */"))
ok("paired: a token with no basename is NOT exempt",
   not rc.paired_allowed("/opt/homebrew/", 'X = "/opt/homebrew/"; Y = "/usr/local/"'))
ok("paired: an unrelated prefix is untouched by the paired rule",
   not rc.paired_allowed("/Users/alice/x", 'A = "/Users/alice/x"; B = "/usr/local/bin/x"'))

# End-to-end through main(), so the `not allowed(...) and not paired_allowed(...)`
# branch is exercised in situ. '/opt/' is not in this file's RC_FLAGS, so add it
# for these two scans and restore, leaving the other cases untouched.
rc.flags.append("/opt/")
try:
    code, out = scan(
        '+++ b/src/a.ts\n'
        '@@ -1,0 +1,1 @@\n'
        '+const FFMPEG = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"];'
    )
    ok("main(): candidate list passes via the paired allow", out.strip() == "")
    code, out = scan(
        '+++ b/src/b.ts\n'
        '@@ -1,0 +1,1 @@\n'
        '+const FFMPEG = "/opt/homebrew/bin/ffmpeg";'
    )
    ok("main(): naked literal is still reported", "/opt/homebrew/bin/ffmpeg" in out)
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Same-line MULTIPLE occurrences (bassilkhilo-ag2, review of head c2802575).
#
# main() scanned only `line.find(p)` — the FIRST occurrence of each flag. That was
# harmless before this PR, because no allow entry ever matched an `/opt/` token, so
# the first occurrence always flagged. This PR adds the first PARTIAL exemption
# (`paired_allowed`), and a first-occurrence-only scan then stops looking once the
# leading token pairs — letting a second, companion-less literal through silently.
#
# Assertions read STDOUT, not main()'s return: main() is a REPORTER and returns 0
# unconditionally (scripts/review-checks.py:235, pre-dates this PR). The verdict is
# the wrapper's — review-checks.sh captures stdout and exits 1 when non-empty. The
# exit-code half of this regression is covered in tests/review-checks.test.sh.
#
# This group appends AFTER the previous block's `rc.flags.remove("/opt/")`, so it
# must re-own the flag; inheriting it would silently disarm every case here.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    _multi = ('+++ b/src/multi.ts\n@@ -1,0 +1,1 @@\n'
              '+const P = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", '
              '"/opt/homebrew/bin/othertool"];')
    _code, _out = scan(_multi)
    # Match the TOKEN FIELD, not the whole report: the format is
    # "<file>:<line>: hardcoded path (<token>): <source line>" — the echoed source
    # line contains every literal on it, so a bare substring test would pass no
    # matter which token was blamed.
    ok("main(): a 2nd, companion-less /opt/ literal on a paired line is reported",
       "hardcoded path (/opt/homebrew/bin/othertool)" in _out)
    ok("main(): ...and the PAIRED token on that same line is not the one blamed",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" not in _out)

    # Controls — the exemption must still exempt, or the fix is just "flag everything".
    _code, _out = scan('+++ b/src/ok.ts\n@@ -1,0 +1,1 @@\n'
                       '+const F = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"];')
    ok("main(): a legitimately paired candidate list still passes", _out.strip() == "")

    _code, _out = scan('+++ b/src/two.ts\n@@ -1,0 +1,1 @@\n'
                       '+const A = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]; '
                       'const B = ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"];')
    ok("main(): TWO independently paired lists on one line both pass", _out.strip() == "")

    _code, _out = scan('+++ b/src/solo.ts\n@@ -1,0 +1,1 @@\n'
                       '+const X = "/opt/homebrew/bin/othertool";')
    ok("main(): the same naked token alone on a line still reports",
       "/opt/homebrew/bin/othertool" in _out)
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Same-basename direct use beside a VALID list (qingyun-wu, review of 0e786f8).
#
# The candidate-list shape is an EXPRESSION, not a line. A line-wide companion
# search let a legitimate list vouch for an unrelated direct use of the SAME
# binary later on the same line — the runtime still launches an Apple-Silicon-only
# path on Intel, which is exactly what this rule exists to prevent. Note the
# every-occurrence fix above is what makes the third token reachable, so this
# case only became observable once that landed.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    _mixed = ('+++ b/src/mixed.ts\n@@ -1,0 +1,1 @@\n'
              '+const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; '
              'spawn("/opt/homebrew/bin/ffmpeg");')
    _code, _out = scan(_mixed)
    ok("main(): direct use beside a valid same-binary list is still reported",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _out)

    # Positive control — the list ALONE must still pass, or the fix is just
    # "stop exempting anything".
    _code, _out = scan('+++ b/src/list.ts\n@@ -1,0 +1,1 @@\n'
                       '+const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];')
    ok("main(): the valid list on its own still passes", _out.strip() == "")

    # Unit level: the companion must be in the token's OWN group.
    _l = 'const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; spawn("/opt/homebrew/bin/ffmpeg");'
    ok("paired: token inside the list is exempt (its group has the companion)",
       rc.paired_allowed("/opt/homebrew/bin/ffmpeg", _l, _l.find("/opt/homebrew/bin/ffmpeg")))
    ok("paired: the direct-use token is NOT exempt (its group has no companion)",
       not rc.paired_allowed("/opt/homebrew/bin/ffmpeg", _l, _l.rfind("/opt/homebrew/bin/ffmpeg")))
    ok("paired: two independently paired groups on one line both exempt",
       rc.paired_allowed("/opt/homebrew/bin/ffprobe",
                         'A=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; '
                         'B=["/opt/homebrew/bin/ffprobe","/usr/local/bin/ffprobe"];',
                         None))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Container semantics (qingyun-wu, review of f3c9751). Two shapes survived the
# innermost-group fix because "innermost group" is not the same question as
# "same candidate list":
#   N1 the direct token's innermost container is an OUTER call whose contents
#      include a NESTED list's companion;
#   N2/N3 the direct token is inside no bracket at all, and the old whole-line
#      fallback then recreated the original same-basename reuse.
# Both are now fail-closed: companion must be a SIBLING at the same depth, and
# no container means no exemption.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _scan1(src):
        return scan('+++ b/src/n.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]

    ok("main(): nested list does not vouch for the outer call's own argument",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _scan1(
           'const x=use(["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"], '
           '"/opt/homebrew/bin/ffmpeg");'))
    ok("main(): bare direct use with NO container is not exempt (comma decls)",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _scan1(
           'const A="/opt/homebrew/bin/ffmpeg", B="/usr/local/bin/ffmpeg", '
           'DIRECT="/opt/homebrew/bin/ffmpeg";'))
    ok("main(): a valid list does not vouch for a bare direct use after it",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _scan1(
           'const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; '
           'const DIRECT="/opt/homebrew/bin/ffmpeg";'))

    # Positive controls — the exemption must survive, or this is just "flag all".
    ok("main(): array candidate list still passes", _scan1(
        'const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];').strip() == "")
    ok("main(): tuple candidate list still passes (python file)",
       scan('+++ b/src/tu1.py\n@@ -1,0 +1,1 @@\n'
            '+C = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")')[1].strip() == "")
    ok("main(): nested candidate list still passes on its OWN tokens", _scan1(
        'const x=use(["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]);').strip() == "")

    # Unit level.
    _n = 'use(["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"], "/opt/homebrew/bin/ffmpeg")'
    ok("paired: sibling-only — nested companion does not exempt the outer arg",
       not rc.paired_allowed("/opt/homebrew/bin/ffmpeg", _n,
                             _n.rfind("/opt/homebrew/bin/ffmpeg")))
    ok("paired: the nested list's OWN token is still exempt",
       rc.paired_allowed("/opt/homebrew/bin/ffmpeg", _n,
                         _n.find("/opt/homebrew/bin/ffmpeg")))
    ok("paired: no bracket container -> never exempt (fail closed)",
       not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                             'A="/opt/homebrew/bin/ffmpeg", B="/usr/local/bin/ffmpeg"'))
    ok("_group_span returns None when the position is in no bracket",
       rc._group_span('A="/opt/x", B="/usr/local/x"', 3) is None)
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# A CALL's argument list is not a candidate collection (qingyun-wu, review of
# c526784). Failing closed on "no container" and blanking nested groups still
# accepted ANY immediate bracket group, so a plain call satisfied the pairing:
#     spawn("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
# The first argument IS the command being launched; the second is just another
# argument, not a fallback the code will ever try.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _s(src):
        return scan('+++ b/src/c.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]

    ok("main(): call-argument siblings do not exempt the command",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in
       _s('spawn("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg");'))
    ok("main(): method-call arguments likewise",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in
       _s('child.exec("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg");'))

    # Controls — genuine collections must still be exempt, or this degrades into
    # "reject everything with a paren".
    ok("main(): array literal still passes",
       _s('const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];').strip() == "")
    ok("main(): python tuple still passes (python file)",
       scan('+++ b/src/tu2.py\n@@ -1,0 +1,1 @@\n'
            '+C = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")')[1].strip() == "")
    ok("main(): a grouping paren after a KEYWORD is not a call",
       _s('    (_p for _p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg")').strip() == "")

    # Unit level.
    # A candidate collection is stated POSITIVELY: an array literal, a tuple, or
    # a sequence-keyword grouping. Everything else is not a list of alternatives
    # the code will try in order.
    ok("container: identifier before ( -> call, not a collection",
       not rc._is_candidate_container('spawn("/a","/b")', 5))
    ok("container: `in` before ( -> sequence context, IS a collection",
       rc._is_candidate_container('for _p in ("/a","/b")', 10))
    ok("container: assignment before ( -> tuple in PYTHON, IS a collection",
       rc._is_candidate_container('C = ("/a","/b")', 4, "src/x.py"))
    ok("container: the same shape in TS is the comma operator, NOT a collection",
       not rc._is_candidate_container('C = ("/a","/b")', 4, "src/x.ts"))
    ok("container: closing bracket before ( -> call, not a collection",
       not rc._is_candidate_container('f()("/a")', 3))
    ok("container: `if` before ( -> condition, NOT a collection",
       not rc._is_candidate_container('if ("/a" || "/b")', 3))
    ok("container: object literal { } is never a collection",
       not rc._is_candidate_container('x = { a: "/a", b: "/b" }', 4))
    ok("container: array literal IS a collection",
       rc._is_candidate_container('C = ["/a","/b"]', 4))
    ok("container: an INDEX is not a collection",
       not rc._is_candidate_container('paths["/a"]', 5))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Non-candidate containers (qingyun-wu, review of 8e83ca5). Rejecting CALL
# parens still left two containers that were never candidate lists either: a
# keyword grouping (`if (...)`) and an object literal. Neither is a sequence the
# code tries in order — the first is a condition, the second keyed config.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _s2(src):
        return scan('+++ b/src/r.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]

    ok("main(): a keyword grouping does not exempt the literal",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _s2(
           'if (cmd === "/opt/homebrew/bin/ffmpeg" || alt === "/usr/local/bin/ffmpeg") run(cmd);'))
    ok("main(): an object literal does not exempt the literal",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in _s2(
           'const cfg = { command: "/opt/homebrew/bin/ffmpeg", '
           'fallbackHint: "/usr/local/bin/ffmpeg" };'))

    # Positive controls preserved.
    ok("main(): array literal still passes",
       _s2('const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];').strip() == "")
    ok("main(): tuple still passes (python file)",
       scan('+++ b/src/t3.py\n@@ -1,0 +1,1 @@\n'
            '+C = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")')[1].strip() == "")
    ok("main(): sequence-keyword generator still passes",
       _s2('    (_p for _p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg")').strip() == "")
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Round 6 (qingyun-wu, review of af236f6). Two opposite defects:
#   (a) FALSE POSITIVES — `_is_candidate_container` rejected `[` whenever any
#       preceding word existed, so `return [...]`, `yield [...]` and
#       `cond and [...]` — ordinary array literals — were flagged. The
#       discriminator for an INDEX is ADJACENCY (`paths[i]`), not the presence
#       of a word somewhere to the left.
#   (b) A MISS — a bare `( ... )` is a tuple in Python but the COMMA OPERATOR in
#       JS/TS, where the value is only the LAST operand. So
#       `("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")` runs the Homebrew
#       path while the /usr/local string is dead, and it was exempt.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _ts(src): return scan('+++ b/src/r6.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]
    def _py(src): return scan('+++ b/src/r6.py\n@@ -1,0 +1,1 @@\n+' + src)[1]
    L = '["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]'

    ok("main(): `return [list]` is a literal, not an index — no false positive",
       _ts("return " + L).strip() == "")
    ok("main(): `yield [list]` likewise", _py("yield " + L).strip() == "")
    ok("main(): `cond and [list]` likewise", _py("paths = cond and " + L).strip() == "")
    ok("main(): an INDEX is still not a candidate collection",
       "hardcoded path" in _ts('const x = paths["/opt/homebrew/bin/ffmpeg"] + alt["/usr/local/bin/ffmpeg"];'))

    ok("main(): a JS comma-operator paren does NOT exempt the command",
       "hardcoded path (/opt/homebrew/bin/ffmpeg)" in
       _ts('const cmd = ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg");'))
    ok("main(): the same shape in PYTHON is a real tuple and still passes",
       _py('cmd = ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")').strip() == "")

    ok("container: `[` at column 0 is a literal (empty-string membership guard)",
       rc._is_candidate_container('["/a","/b"]', 0))
    ok("container: `[` adjacent to an identifier is an index",
       not rc._is_candidate_container('paths["/a"]', 5))
    ok("container: `[` after a keyword AND a space is a literal",
       rc._is_candidate_container('return ["/a","/b"]', 7))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Round 7 (qingyun-wu, review of 7b94efc). Adjacency alone cannot separate an
# INDEX from an ARRAY LITERAL: both languages allow whitespace before a
# subscript, and JS has optional element access. The discriminator is KEYWORD vs
# IDENTIFIER — `return [...]` is a literal, `paths [...]` is a lookup.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _t7(src): return scan('+++ b/src/r7.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]
    P = '["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]'
    ok("main(): whitespace before an index is still an index",
       "hardcoded path" in _t7("const cmd = paths " + P + ";"))
    ok("main(): optional element access `?.[` is an index",
       "hardcoded path" in _t7("const cmd = paths?." + P + ";"))
    ok("main(): `return [list]` remains a literal", _t7("return " + P).strip() == "")
    ok("container: identifier + space before [ -> index",
       not rc._is_candidate_container('paths ["/a","/b"]', 6))
    ok("container: `?.[` -> index",
       not rc._is_candidate_container('paths?.["/a","/b"]', 7))
    ok("container: keyword + space before [ -> literal",
       rc._is_candidate_container('return ["/a","/b"]', 7))
    # `)` / `]` before the bracket: a subscript on an expression result, e.g.
    # `fn()["k"]` or `rows[0]["k"]`. Distinct from the `?.` and identifier arms.
    ok("container: `)` before [ -> subscript on a call result",
       not rc._is_candidate_container('fn()["/a","/b"]', 4))
    ok("container: `]` before [ -> chained subscript",
       not rc._is_candidate_container('rows[0]["/a","/b"]', 7))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Round 9 (qingyun-wu, review of 3d631b7). A JS member expression continues
# across a newline, so a `[` that STARTS its line can still be a subscript:
#     const cmd = paths
#     ["/usr/local/...", "/opt/homebrew/..."];
# The scanner is line-based, so it now carries the previous ADDED line's
# executable text and treats a line-start `[` as an index when that line ends in
# something a subscript attaches to. Reset per file and per hunk — across a hunk
# gap the preceding line is unknown, and unknown must not read as "standalone".
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    P = '["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]'
    ok("main(): a line-continued subscript is still an index",
       "hardcoded path" in scan('+++ b/src/c9.ts\n@@ -1,0 +1,2 @@\n+const cmd = paths\n+' + P + ';')[1])
    ok("main(): a genuinely standalone line-start literal still passes",
       scan('+++ b/src/c9b.ts\n@@ -1,0 +1,2 @@\n+const a = 1;\n+'
            '["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"];')[1].strip() == "")
    ok("container: prev line ending in an identifier -> continuation, not literal",
       not rc._is_candidate_container('["/a","/b"]', 0, "x.ts", "const cmd = paths"))
    ok("container: prev line ending in `;` -> genuinely standalone literal",
       rc._is_candidate_container('["/a","/b"]', 0, "x.ts", "const a = 1;"))
    ok("container: no previous line (hunk start) -> literal, as before",
       rc._is_candidate_container('["/a","/b"]', 0, "x.ts", None))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Round 10 (qingyun-wu, review of 3d631b7). A candidate-LOOKING literal that is
# immediately SUBSCRIPTED is not a fallback list:
#     const cmd = ["/usr/local/...", "/opt/homebrew/..."][1];
# The index picks one operand at AUTHOR time, so the other string is dead and the
# selected one runs unconditionally. Distinct from the line-continuation case:
# there the bracket was the subscript; here the literal itself is the subject.
#
# `.find(...)` / `.filter(...)` deliberately still pass — they choose at RUNTIME
# by probing, which is what makes a candidate list portable in the first place.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    def _x(src): return scan('+++ b/src/r10.ts\n@@ -1,0 +1,1 @@\n+' + src)[1]
    L = '["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]'
    ok("main(): an immediately-indexed literal is not a fallback list",
       "hardcoded path" in _x("const cmd = " + L + "[1];"))
    ok("main(): [0] selects just as deterministically",
       "hardcoded path" in _x("const cmd = " + L + "[0];"))
    ok("main(): .find(exists) is a RUNTIME resolver and still passes",
       _x('const cmd = ["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"].find(exists);').strip() == "")
    ok("main(): a plain list still passes",
       _x('const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];').strip() == "")
    ok("_is_selected_from: subscript immediately after the close",
       rc._is_selected_from('["/a","/b"][1]', 10))
    ok("_is_selected_from: whitespace then subscript still counts",
       rc._is_selected_from('["/a","/b"] [1]', 10))
    ok("_is_selected_from: a semicolon does not count",
       not rc._is_selected_from('["/a","/b"];', 10))
    ok("_is_selected_from: a method call does not count",
       not rc._is_selected_from('["/a","/b"].find(x)', 10))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Rounds 11-13 (qingyun-wu, review of 527b8687). Three ways a candidate-looking
# list is still selected deterministically:
#   A. the subscript opens the NEXT line (JS continues the member expression);
#   B. `.at(1)` — a method that selects rather than probes;
#   C. the expression is unchanged CONTEXT and only the bracket line is added, so
#      `prev_added` was never populated and the bracket looked standalone.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    L = '["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]'
    ok("main(): a subscript opening the NEXT line still selects",
       "hardcoded path" in scan('+++ b/src/n1.ts\n@@ -0,0 +1,2 @@\n+const cmd = ' + L + '\n+[1];')[1])
    ok("main(): `.at(1)` selects — not a runtime resolver",
       "hardcoded path" in scan('+++ b/src/n2.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.at(1);')[1])
    ok("main(): a CONTEXT expression + added bracket line is a continuation",
       "hardcoded path" in scan('+++ b/src/n3.ts\n@@ -1 +1,2 @@\n const cmd = paths\n+' + L + ';')[1])

    ok("main(): `.find(exists)` is still a runtime resolver",
       scan('+++ b/src/n4.ts\n@@ -1,0 +1,1 @@\n+const cmd = '
            '["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"].find(exists);')[1].strip() == "")
    ok("main(): a standalone line-start literal still passes",
       scan('+++ b/src/n5.ts\n@@ -1,0 +1,2 @@\n+const a = 1;\n+'
            '["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"];')[1].strip() == "")

    ok("_is_selected_from: next-line subscript counts",
       rc._is_selected_from('["/a","/b"]', 10, "[1];"))
    ok("_is_selected_from: next line that is NOT a subscript does not",
       not rc._is_selected_from('["/a","/b"]', 10, "const x = 1;"))
    ok("_is_selected_from: an unknown method fails CLOSED",
       rc._is_selected_from('["/a","/b"].at(1)', 10))
    ok("_is_selected_from: a resolver method is permitted",
       not rc._is_selected_from('["/a","/b"].find(x)', 10))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Round 14 (qingyun-wu, review of 8c6859b3). A PASS-THROUGH transform ended the
# chain scan, so a deterministic selector one link further down was never seen:
#
#     ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => x)[1]
#
# `map` is 1:1, so that `[1]` is the same AUTHOR-time pick as indexing the
# literal directly. The split is by what the method does to the candidate SET:
# a probe narrows it at runtime (so a subscript on its result is still runtime-
# determined — `.filter(exists)[0]`), a transform does not (so the chain must be
# read on). Unknown methods keep failing CLOSED.
# ---------------------------------------------------------------------------
rc.flags.append("/opt/")
try:
    L = '["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]'
    ok("main(): `.map(x => x)[1]` selects through the transform",
       "hardcoded path" in scan('+++ b/src/m1.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.map(x => x)[1];')[1])
    ok("main(): `.flatMap(...)[0]` selects through the transform",
       "hardcoded path" in scan('+++ b/src/m2.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.flatMap(x => [x])[0];')[1])
    ok("main(): `.map(...).at(1)` selects through the transform",
       "hardcoded path" in scan('+++ b/src/m3.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.map(x => x).at(1);')[1])

    ok("main(): `.map(...).find(exists)` still resolves at runtime",
       scan('+++ b/src/m4.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.map(x => x).find(exists);')[1].strip() == "")
    ok("main(): `.filter(exists)[0]` is a probe, not a selection",
       scan('+++ b/src/m5.ts\n@@ -1,0 +1,1 @@\n+const cmd = ' + L + '.filter(exists)[0];')[1].strip() == "")

    ok("_is_selected_from: transform then subscript selects",
       rc._is_selected_from('["/a","/b"].map(x => x)[1]', 10))
    ok("_is_selected_from: transform then probe is permitted",
       not rc._is_selected_from('["/a","/b"].map(x => x).find(e)', 10))
    ok("_is_selected_from: transform whose args nest parens is still walked",
       rc._is_selected_from('["/a","/b"].map(f(a, b)).at(0)', 10))
    ok("_is_selected_from: a probe still ends the chain",
       not rc._is_selected_from('["/a","/b"].filter(e)[0]', 10))

    # `_call_end` returns None for a shape it cannot read; the caller must treat
    # that as UNKNOWN and fail closed, never as "resolved".
    ok("_call_end: nested parens close at the outer paren",
       rc._call_end('.map(f(a, b))x', 4) == 12)
    ok("_call_end: no call parens is unreadable", rc._call_end('.map x', 4) is None)
    ok("_call_end: an unbalanced call is unreadable", rc._call_end('.map(f(a, b)', 4) is None)
    ok("_is_selected_from: an unreadable transform chain fails CLOSED",
       rc._is_selected_from('["/a","/b"].map', 10))
finally:
    rc.flags.remove("/opt/")

# ---------------------------------------------------------------------------
# Branch coverage for the container predicate. The diff-coverage gate flagged
# these ten lines; each is a real branch the behavioural cases never reach
# because they all take an earlier return. Asserted directly at the predicate so
# a future edit to any arm fails here rather than silently.
# ---------------------------------------------------------------------------
ok("_code_part: a backslash escape inside a string does not end the string",
   rc._code_part('x = "a\\"# still in string"; y = 1') == 'x = "a\\"# still in string"; y = 1')
ok("_prev_word: nothing before the position -> empty char and word",
   rc._prev_word("(a)", 0) == ("", ""))
ok("_is_candidate_container: a non-bracket character is never a container",
   not rc._is_candidate_container("x = 1", 0))
ok("_siblings_only: a NESTED group is blanked, siblings survive",
   rc._siblings_only('(a, [b], c)', 0, 10) == 'a,    , c')   # nested [b] -> 3 blanks
ok("paired: a position past the code part (inside a comment) is not exempt",
   not rc.paired_allowed("/opt/homebrew/bin/ffmpeg",
                         'x = 1  # ["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]',
                         30))
ok("_is_candidate_container: a list at COLUMN 0 is still a container",
   rc._is_candidate_container('["/a","/b"]', 0))
ok("_is_candidate_container: a paren at COLUMN 0 is still a tuple (python)",
   rc._is_candidate_container('("/a","/b")', 0, "src/x.py"))
ok("paired: a token under NO configured paired prefix falls through the loop",
   not rc.paired_allowed("/Users/alice/ffmpeg",
                         '["/Users/alice/ffmpeg", "/usr/local/bin/ffmpeg"]', 2))

print("---")
if failed:
    print("FAILED — %d of %d" % (failed, passed + failed))
    sys.exit(1)
print("PASS — review-checks.py internals (%d checks)" % passed)
sys.exit(0)
