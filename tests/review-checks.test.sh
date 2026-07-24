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

# --- file-skip: path literals are legit DATA in docs/tests/runner -------------
check "docs (.md) not scanned"                        clean $'+++ b/docs/x.md\n@@ -1,0 +1,1 @@\n+example path: /Users/a/b'
check "tests/ not scanned"                            clean $'+++ b/tests/x.test.sh\n@@ -1,0 +1,1 @@\n+D="/Users/alice/app"'
check "code still flagged alongside skipped file"     flag  $'+++ b/docs/a.md\n@@ -1,0 +1,1 @@\n+see /Users/x\n+++ b/src/a.ts\n@@ -1,0 +1,1 @@\n+const p="/home/y";'

# --- guide resolution + fallback ---------------------------------------------
check "explicit --guide is honored"                   flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/opt/thing";' --guide "$GUIDE"
check "missing guide falls back, still flags /Users/" flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/Users/a/b";' --guide /does/not/exist
check "empty diff exits 0 (nothing to check)"         clean $''

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
