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
TMPD="$(mktemp -d -t review-checks-test.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT
# check <name> <expect: flag|clean|<numeric rc>> <diff> [extra-args...]
check() {
    local name="$1" expect="$2" diff="$3"; shift 3
    local out rc want
    case "$expect" in flag) want=1;; clean) want=0;; *) want="$expect";; esac
    out="$(printf '%s' "$diff" | bash "$RUNNER" "$@" 2>/dev/null)"; rc=$?
    if [ "$rc" = "$want" ]; then
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
# The send-allowlist tokens are exempt, but ONLY those two: a sibling path under
# the same root must still flag, or the allow would blanket /private/tmp.
check "allowed /private/tmp/sutando- token"           clean $'+++ b/a.py\n@@ -1,0 +1,1 @@\n+    "/private/tmp/sutando-",'
check "allowed /private/tmp/echo- token"              clean $'+++ b/a.py\n@@ -1,0 +1,1 @@\n+    "/private/tmp/echo-",'
check "other /private/tmp/ path still flagged"        flag  $'+++ b/a.py\n@@ -1,0 +1,1 @@\n+    p = "/private/tmp/unrelated-cache"'
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

# Container semantics: the companion must be a SIBLING in the same immediate
# list, and a token in no container is never exempt (review of f3c9751).
check "nested list does not vouch for outer arg"        flag  $'+++ b/skills/x/n1.ts\n@@ -1,0 +1,1 @@\n+const x=use(["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"], "/opt/homebrew/bin/ffmpeg");'
check "no-container direct use is not exempt"           flag  $'+++ b/skills/x/n2.ts\n@@ -1,0 +1,1 @@\n+const A="/opt/homebrew/bin/ffmpeg", B="/usr/local/bin/ffmpeg", DIRECT="/opt/homebrew/bin/ffmpeg";'
check "valid list does not vouch for a later bare use"  flag  $'+++ b/skills/x/n3.ts\n@@ -1,0 +1,1 @@\n+const C=["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]; const DIRECT="/opt/homebrew/bin/ffmpeg";'
check "control: tuple candidate list still passes"      clean $'+++ b/skills/x/n4.py\n@@ -1,0 +1,1 @@\n+C = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")'
check "control: nested list passes on its own tokens"   clean $'+++ b/skills/x/n5.ts\n@@ -1,0 +1,1 @@\n+const x=use(["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"]);'

# A call's argument list is not a candidate collection (review of c526784).
check "call-arg siblings do not exempt the command"     flag  $'+++ b/skills/x/c1.ts\n@@ -1,0 +1,1 @@\n+spawn("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg");'
check "method-call arguments likewise"                  flag  $'+++ b/skills/x/c2.ts\n@@ -1,0 +1,1 @@\n+child.exec("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg");'
check "control: grouping paren after a keyword passes"  clean $'+++ b/skills/x/c3.py\n@@ -1,0 +1,1 @@\n+    (_p for _p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg")'

# Non-candidate containers: a keyword grouping is a condition, an object literal
# is keyed config — neither is a sequence tried in order (review of 8e83ca5).
check "keyword grouping does not exempt"                flag  $'+++ b/skills/x/r1.ts\n@@ -1,0 +1,1 @@\n+if (cmd === "/opt/homebrew/bin/ffmpeg" || alt === "/usr/local/bin/ffmpeg") run(cmd);'
check "object literal does not exempt"                  flag  $'+++ b/skills/x/r2.ts\n@@ -1,0 +1,1 @@\n+const cfg = { command: "/opt/homebrew/bin/ffmpeg", fallbackHint: "/usr/local/bin/ffmpeg" };'

# Round 6: an INDEX is adjacency-based, so `return [...]` must NOT false-flag; and
# a bare paren is a tuple in Python but the COMMA OPERATOR in JS/TS.
check "return [list] is a literal, not an index"        clean $'+++ b/skills/x/r61.ts\n@@ -1,0 +1,1 @@\n+return ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]'
check "cond and [list] likewise"                        clean $'+++ b/skills/x/r62.py\n@@ -1,0 +1,1 @@\n+paths = cond and ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]'
check "JS comma-operator paren does not exempt"         flag  $'+++ b/skills/x/r63.ts\n@@ -1,0 +1,1 @@\n+const cmd = ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg");'
check "python tuple of the same shape still passes"     clean $'+++ b/skills/x/r64.py\n@@ -1,0 +1,1 @@\n+cmd = ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")'

# Round 7: whitespace indexing and optional element access are lookups, not literals.
check "whitespace before [ is still an index"           flag  $'+++ b/skills/x/r71.ts\n@@ -1,0 +1,1 @@\n+const cmd = paths ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"];'
check "optional element access ?.[ is an index"         flag  $'+++ b/skills/x/r72.ts\n@@ -1,0 +1,1 @@\n+const cmd = paths?.["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"];'

# Round 9: a member expression can continue across a newline.
check "line-continued subscript is still an index"      flag  $'+++ b/skills/x/c9.ts\n@@ -1,0 +1,2 @@\n+const cmd = paths\n+["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"];'
check "standalone line-start literal still passes"      clean $'+++ b/skills/x/c9b.ts\n@@ -1,0 +1,2 @@\n+const a = 1;\n+["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"];'

# Round 10: an immediately-subscripted literal selects one operand at author time.
check "immediately-indexed literal is not a list"      flag  $'+++ b/skills/x/x1.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"][1];'
check "runtime .find() resolver still passes"          clean $'+++ b/skills/x/x2.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/opt/homebrew/bin/ffmpeg","/usr/local/bin/ffmpeg"].find(exists);'

# Rounds 11-13: next-line subscript, .at() selection, context-line continuation.
check "next-line subscript still selects"              flag  $'+++ b/skills/x/n1.ts\n@@ -0,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+[1];'
check ".at(1) selects, not a resolver"                 flag  $'+++ b/skills/x/n2.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].at(1);'
check "context expr + added bracket is a continuation" flag  $'+++ b/skills/x/n3.ts\n@@ -1 +1,2 @@\n const cmd = paths\n+["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"];'

# `"" in ")]"` is True in Python, so an unguarded prev-char test made a bracket at
# COLUMN 0 read as a call on a returned value and falsely flagged a valid list.
check "candidate list at column 0 still passes"         clean $'+++ b/skills/x/col0.ts\n@@ -1,0 +1,1 @@\n+["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]'

# Regex-vs-division at the WRAPPER level. Guessing "regex" wrongly blanks the rest
# of the line, so `_call_end` fails closed and VALID portable code is flagged —
# the asymmetry this check exists to remove. The identifier case is the ordinary
# control; postfix `++`/`--` and `}` are expression-enders that a prev-char-only
# test misreads as operators. The last row is the one that matters most: the same
# postfix division with an AUTHOR-TIME index must still flag, so fixing the false
# positive did not open a bypass.
check "identifier division + probe passes"              clean $'+++ b/skills/x/d1.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => (n / 2, x)).find(exists);'
check "postfix ++ division + probe passes"              clean $'+++ b/skills/x/d2.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => (n++ / 2, x)).find(exists);'
check "postfix -- division + probe passes"              clean $'+++ b/skills/x/d3.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => (n-- / 2, x)).find(exists);'
check "brace-ended expression division + probe passes"  clean $'+++ b/skills/x/d4.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => ({} / 2, x)).find(exists);'
check "a single + still opens a regex"                  clean $'+++ b/skills/x/d5.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => (a + /\\)/.source, x)).find(exists);'
check "postfix division + author-time index still flags" flag $'+++ b/skills/x/d6.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => (n++ / 2, x))[1];'

# A method chain CONTINUING ON THE NEXT LINE was invisible: only a bare `[`
# opener was followed, so `.map(...)[1]` split across the break went unread and
# the list was exempted. Present since this branch first added the paired allow,
# NOT introduced by the `}` change — the minimal shape below has no brace and no
# regex and bypassed identically at every head of this PR.
check "2-line chain: transform then index selects"     flag  $'+++ b/skills/x/L1.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)[1];'
check "2-line chain: selector method selects"          flag  $'+++ b/skills/x/L2.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .at(1);'
check "2-line chain: block-close regex then index"     flag  $'+++ b/skills/x/L3.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => { if (x) {} /\\)/.test(x); return x; })[1];'
check "2-line chain: transform then probe passes"      clean $'+++ b/skills/x/L4.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x).find(exists);'
check "2-line chain: probe alone passes"               clean $'+++ b/skills/x/L5.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .find(exists);'
check "1-line block-close regex then index selects"    flag  $'+++ b/skills/x/L6.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => { if (x) {} /\\)/.test(x); return x; })[1];'
check "object-literal division + index still selects"  flag  $'+++ b/skills/x/L7.ts\n@@ -1,0 +1,1 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"].map(x => ({} / 2, x))[1];'

# The chain must be carried until it RESOLVES or TERMINATES, not for a fixed
# number of physical lines. Bounding it at one lookahead line meant ordinary
# formatting — `.map(...)` and `[1]` on separate lines — slipped past.
check "3-line chain: selector on the third line"       flag  $'+++ b/skills/x/M1.ts\n@@ -1,0 +1,3 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+  [1];'
check "3-line chain: selector METHOD on the third line" flag $'+++ b/skills/x/M2.ts\n@@ -1,0 +1,3 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+  .at(1);'
check "4-line chain still reaches the selector"        flag  $'+++ b/skills/x/M3.ts\n@@ -1,0 +1,4 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+  .map(y => y)\n+  [1];'
check "a blank line does not terminate the chain"      flag  $'+++ b/skills/x/M4.ts\n@@ -1,0 +1,4 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+\n+  [1];'
check "3-line chain ending in a PROBE passes"          clean $'+++ b/skills/x/M5.ts\n@@ -1,0 +1,3 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+  .find(exists);'
check "4-line chain ending in a PROBE passes"          clean $'+++ b/skills/x/M6.ts\n@@ -1,0 +1,4 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+  .map(x => x)\n+  .map(y => y)\n+  .find(exists);'
check "a NON-continuation next line ends the chain"    clean $'+++ b/skills/x/M7.ts\n@@ -1,0 +1,2 @@\n+const cmd = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]\n+const other = 1;'

# --- file-skip: path literals are legit DATA in docs/tests/runner -------------
check "docs (.md) not scanned"                        clean $'+++ b/docs/x.md\n@@ -1,0 +1,1 @@\n+example path: /Users/a/b'
check "tests/ not scanned"                            clean $'+++ b/tests/x.test.sh\n@@ -1,0 +1,1 @@\n+D="/Users/alice/app"'
check "code still flagged alongside skipped file"     flag  $'+++ b/docs/a.md\n@@ -1,0 +1,1 @@\n+see /Users/x\n+++ b/src/a.ts\n@@ -1,0 +1,1 @@\n+const p="/home/y";'

# --- guide resolution + fallback ---------------------------------------------
check "explicit --guide is honored"                   flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/opt/thing";' --guide "$GUIDE"
check "missing guide falls back, still flags /Users/" flag  $'+++ b/z.ts\n@@ -1,0 +1,1 @@\n+const p="/Users/a/b";' --guide /does/not/exist
# --- empty input is "nothing was SCANNED", not "nothing was FOUND" -----------
# Exit 0 let a no-op read as a clean gate to callers that check only the status.
check "empty stdin fails closed (rc=2, never a pass)"  2     $''
check "whitespace-only stdin fails closed too"         2     $' \n\t\n'
check "--allow-empty opts an empty input back into 0"  clean $'' --allow-empty
# --diff is the CI call shape, so cover the empty FILE path too, not just stdin.
: > "$TMPD/empty.diff"
bash "$RUNNER" --diff "$TMPD/empty.diff" >/dev/null 2>&1; empty_file_rc=$?
if [ "$empty_file_rc" = 2 ]; then
    echo "ok   empty --diff file fails closed (rc=2)"; pass=$((pass+1))
else
    echo "FAIL empty --diff file rc=$empty_file_rc, want 2"; fail=$((fail+1))
fi
# The runner must not print its PASS line on any empty-input path.
for _a in "" "--allow-empty"; do
    _o="$(printf '' | bash "$RUNNER" $_a 2>/dev/null)"
    if [ -z "$_o" ]; then
        echo "ok   empty input prints no PASS line (args='$_a')"; pass=$((pass+1))
    else
        echo "FAIL empty input printed to stdout (args='$_a'): '$_o'"; fail=$((fail+1))
    fi
done

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
