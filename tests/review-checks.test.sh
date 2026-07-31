#!/usr/bin/env bash
# Tests for scripts/review-checks.sh — the guide-driven review-checks runner.
# Folds the hardcoded-path scanner fixtures from #2229 (which this supersedes)
# and adds coverage for: guide-supplied patterns, no-guide fallback, per-token
# allow anchoring, and the file-skip (docs/tests/runner carry pattern literals
# as data, not shipping code).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$HERE/../scripts/review-checks.sh"
GUIDE="$HERE/../REVIEW.md"

pass=0; fail=0
# check <name> <expect: flag|clean> <diff> [extra-args...]
check() {
    local name="$1" expect="$2" diff="$3"; shift 3
    local out rc
    out="$(printf '%s' "$diff" | bash "$RUNNER" "$@" 2>/dev/null)"; rc=$?
    if { [ "$expect" = flag ] && [ "$rc" = 1 ]; } || { [ "$expect" = clean ] && [ "$rc" = 0 ]; }; then
        echo "ok   $name"; pass=$((pass+1))
    else
        echo "FAIL $name (rc=$rc, want=$expect)"; fail=$((fail+1))
    fi
}

# --- hardcoded-paths scanner (fixtures carried from #2229) ---------------------
check "positive /Users/ path flagged"                 flag  $'+++ b/app.js\n@@ -1,0 +1,1 @@\n+const home = "/Users/alice/app";'
check "# and // comment lines not flagged"            clean $'+++ b/c.js\n@@ -1,0 +1,2 @@\n+# note /Users/bob/x\n+// note /Users/bob/x'
check "fixture paths (/usr/fake,/nonexistent,/tmp)"   clean $'+++ b/f.js\n@@ -1,0 +1,3 @@\n+a = "/usr/fake/p"\n+b = "/nonexistent/p"\n+c = "/tmp/scratch"'
check "mixed forbidden+allowed line still flags real" flag  $'+++ b/m.js\n@@ -1,0 +1,1 @@\n+x = "/Users/alice/app"; y = "https://example.com";'
check "real /Users/tmp/ not masked by /tmp/ allow"    flag  $'+++ b/t.js\n@@ -1,0 +1,1 @@\n+p = "/Users/tmp/keep"'
check "~/.claude flagged"                             flag  $'+++ b/g.sh\n@@ -1,0 +1,1 @@\n+cfg=~/.claude/settings.json'
check "/home/ flagged"                                flag  $'+++ b/i.py\n@@ -1,0 +1,1 @@\n+p = "/home/bob/.config"'
check "clean diff passes"                             clean $'+++ b/h.js\n@@ -1,0 +1,1 @@\n+const x = resolveWorkspace();'

# --- paired allow: portable candidate list vs naked arch-specific literal -----
# The exemption for '/opt/homebrew/' is CONTEXTUAL (REVIEW.md allow_paired): the
# token passes only beside a same-named companion under the companion prefix.
# Both directions are asserted — an exception that only proves its allow is a
# guard that cannot say NO.
check "candidate-list: TS array passes"               clean $'+++ b/skills/x/a.ts\n@@ -1,0 +1,1 @@\n+const FFMPEG = [\'/opt/homebrew/bin/ffmpeg\', \'/usr/local/bin/ffmpeg\', \'ffmpeg\']'
check "candidate-list: py generator passes"           clean $'+++ b/skills/x/r.py\n@@ -1,0 +1,1 @@\n+    (_p for _p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg")'
check "naked /opt/homebrew literal still flagged"     flag  $'+++ b/skills/x/b.ts\n@@ -1,0 +1,1 @@\n+const FFMPEG = "/opt/homebrew/bin/ffmpeg";'
check "unrelated /opt/homebrew binary still flagged"  flag  $'+++ b/skills/x/c.py\n@@ -1,0 +1,1 @@\n+BIN = "/opt/homebrew/bin/python3"'
check "coincidental /usr/local does not exempt"       flag  $'+++ b/skills/x/e.py\n@@ -1,0 +1,1 @@\n+A = "/opt/homebrew/bin/ffmpeg"; B = "/usr/local/share/doc"'
check "mismatched companion basename does not exempt" flag  $'+++ b/skills/x/g.py\n@@ -1,0 +1,1 @@\n+A = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffprobe")'
check "paired line still flags a real /Users/ leak"   flag  $'+++ b/skills/x/f.py\n@@ -1,0 +1,1 @@\n+A = ("/opt/homebrew/bin/x","/usr/local/bin/x"); L = "/Users/alice/s"'

# Second occurrence of the SAME prefix (bassilkhilo-ag2, review of head c2802575).
# Distinct from the /Users/ case directly above: that leak is a DIFFERENT flag, so
# the outer per-prefix loop reaches it. Here both literals are '/opt/homebrew/', and
# the scan used to inspect only the first — which pairs — and stop, so the naked
# second token was never examined. Asserted at the wrapper level because the wrapper
# owns the verdict (review-checks.py always returns 0; review-checks.sh exits 1 on
# non-empty stdout), so this covers the exit code the CI gate actually reads.
check "2nd same-prefix token, unpaired, still flagged" flag  $'+++ b/skills/x/h.ts\n@@ -1,0 +1,1 @@\n+const P = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/othertool"];'
check "two independently paired lists both pass"       clean $'+++ b/skills/x/i.ts\n@@ -1,0 +1,1 @@\n+const A=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; const B=["/opt/homebrew/bin/ffprobe","/usr/local/bin/ffprobe"];'

# Companion must be in the token's OWN group, not merely the same line: a valid
# list must not vouch for an unrelated direct use of the same binary (review of
# 0e786f8). origin/main flags this line; the line-wide version passed it.
check "direct use beside a valid same-binary list flags" flag  $'+++ b/skills/x/j.ts\n@@ -1,0 +1,1 @@\n+const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; spawn("/opt/homebrew/bin/ffmpeg");'
check "positive control: that same list alone passes"   clean $'+++ b/skills/x/k.ts\n@@ -1,0 +1,1 @@\n+const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"];'

# --- file-skip: path literals are legit DATA in docs/tests/runner -------------
check "docs (.md) not scanned"                        clean $'+++ b/docs/x.md\n@@ -1,0 +1,1 @@\n+example path: /Users/a/b'
check "tests/ not scanned"                            clean $'+++ b/tests/x.test.sh\n@@ -1,0 +1,1 @@\n+D="/Users/alice/app"'
check "code still flagged alongside skipped file"     flag  $'+++ b/docs/a.md\n@@ -1,0 +1,1 @@\n+see /Users/x\n+++ b/src/a.ts\n@@ -1,0 +1,1 @@\n+const p="/home/y";'

# --- guide resolution + fallback ---------------------------------------------
check "explicit --guide is honored"                   flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/opt/thing";' --guide "$GUIDE"
check "missing guide falls back, still flags /Users/" flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/Users/a/b";' --guide /does/not/exist
check "empty diff exits 0 (nothing to check)"         clean $''

# --- oversized input can't silently bypass the scan (#2281) -------------------
# A diff far larger than the OS argv/env limit (~1MB on macOS) used to be handed
# to the Python scanner via the RC_DIFF env var, which blew 'Argument list too
# long' — the scanner never launched, yet the runner still printed PASS/exit 0.
# Streamed via stdin the embedded hardcoded path must still be flagged (exit 1),
# and it must never silently PASS.
big_out="$( { printf '+++ b/big.js\n@@ -1,0 +1,200001 @@\n'; \
              yes '+const filler = resolveWorkspace();' | head -n 200000; \
              printf '+const home = "/Users/alice/secret";\n'; } \
            | bash "$RUNNER" 2>/dev/null )"; big_rc=$?
if [ "$big_rc" = 1 ] && [ -z "$big_out" ]; then
    echo "ok   oversized diff still flags (no silent PASS)"; pass=$((pass+1))
else
    echo "FAIL oversized diff bypassed scan (rc=$big_rc, stdout='$big_out')"; fail=$((fail+1))
fi

# --- guide's own checks: block is parseable ----------------------------------
if grep -q "hardcoded-paths:" "$GUIDE" && grep -qE "^\s*flag:" "$GUIDE"; then
    echo "ok   REVIEW.md carries a hardcoded-paths checks: block"; pass=$((pass+1))
else
    echo "FAIL REVIEW.md missing checks: block"; fail=$((fail+1))
fi

echo "---"
if [ "$fail" -eq 0 ]; then
    echo "PASS — review-checks runner ($pass checks)"
    exit 0
else
    echo "FAILED — $fail of $((pass+fail)) checks"
    exit 1
fi
