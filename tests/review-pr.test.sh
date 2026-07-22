#!/usr/bin/env bash
# Tests for skills/claude-codex/scripts/review-pr.sh — the read-only PR-review helper.
# Stubs `gh` and `codex` on PATH so the test runs offline (no network, no model).
#   bash tests/review-pr.test.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/skills/claude-codex/scripts/review-pr.sh"
fail=0
check(){ if [ "$2" = "$3" ]; then printf 'ok   - %s\n' "$1"; else printf 'FAIL - %s (want=%q got=%q)\n' "$1" "$2" "$3"; fail=1; fi; }

# --- stub dir on PATH ---
STUB="$(mktemp -d)"
trap 'rm -rf "$STUB"' EXIT

# stub `gh`: `gh pr diff <N>` prints a fake diff for N!=999, empty for 999
cat > "$STUB/gh" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "pr" ] && [ "$2" = "diff" ]; then
  [ "$3" = "999" ] && exit 0   # empty diff
  printf 'diff --git a/x b/x\n+added line\n'
  exit 0
fi
exit 1
SH
chmod +x "$STUB/gh"

# stub `codex`: find `-o <path>`, write a fake verdict there, exit 0
cat > "$STUB/codex" <<'SH'
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out="$2"; shift 2; continue; }; shift; done
[ -n "$out" ] && printf -- '- STUB VERDICT: no blocking issues\n' > "$out"
exit 0
SH
chmod +x "$STUB/codex"

PATH="$STUB:$PATH"

# 1. usage error when no PR number
bash "$HELPER" >/dev/null 2>&1; check "no arg → exit 2 (usage)" 2 "$?"

# 2. unknown flag → exit 2
bash "$HELPER" 1754 --bogus >/dev/null 2>&1; check "unknown flag → exit 2" 2 "$?"

# 3. empty diff (#999) → exit 2 with message
bash "$HELPER" 999 >/dev/null 2>&1; check "empty diff → exit 2" 2 "$?"

# 4. happy path: prints the stubbed verdict, exit 0
out="$(bash "$HELPER" 1754 2>/dev/null)"; rc=$?
check "happy path → exit 0" 0 "$rc"
case "$out" in *"STUB VERDICT"*) v=ok;; *) v=missing;; esac
check "happy path prints codex verdict" ok "$v"

# 5. --max is accepted (and forwarded; happy path still works)
bash "$HELPER" 1754 --max 300 >/dev/null 2>&1; check "--max accepted → exit 0" 0 "$?"

# --- scanner fixtures: scan_hardcoded_paths, the mandatory hardcoded-path signal ---
# Source just the function (extracted from the helper) so we can drive it with
# crafted diffs. Deterministic — no gh/codex. Covers the review findings on the
# first cut: whole-line exclusion masking, bare-`*` over-match, and $2 truncation.
awk '/^scan_hardcoded_paths\(\) \{/,/^\}/' "$HELPER" > "$STUB/scanfn.sh"
# shellcheck disable=SC1090
source "$STUB/scanfn.sh"
scan(){ printf '%s\n' "$1" | scan_hardcoded_paths; }

# a) a positive absolute path is flagged, reported as file:line off the +++/@@ headers
D=$'+++ b/app.js\n@@ -1,0 +1,1 @@\n+const home = "/Users/alice/app";'
case "$(scan "$D")" in "app.js:1: const home"*) v=ok;; *) v=miss;; esac
check "positive /Users/ path flagged as file:line" ok "$v"

# b) # and // comment lines are NOT flagged
D=$'+++ b/c.js\n@@ -1,0 +1,2 @@\n+# note /Users/bob/x\n+// note /Users/bob/x'
check "# and // comment lines not flagged" "" "$(scan "$D")"

# c) allowed fixture / system-noise paths are NOT flagged
D=$'+++ b/f.js\n@@ -1,0 +1,3 @@\n+a = "/usr/fake/p"\n+b = "/nonexistent/p"\n+c = "/tmp/scratch"'
check "fixture paths (/usr/fake,/nonexistent,/tmp) not flagged" "" "$(scan "$D")"

# d) FIX (john-the-dev): a real /Users/ path sharing a line with example.com / a
#    fixture token still flags — the exclusion is per-token, not whole-line.
D=$'+++ b/m.js\n@@ -1,0 +1,1 @@\n+x = "/Users/alice/app"; y = "https://example.com";'
case "$(scan "$D")" in *"/Users/alice/app"*) v=ok;; *) v=miss;; esac
check "mixed forbidden+allowed line still flags the real path" ok "$v"
#    ...and the anchored /tmp/ exclusion does NOT swallow a real /Users/tmp/ path
D=$'+++ b/t.js\n@@ -1,0 +1,1 @@\n+p = "/Users/tmp/keep"'
case "$(scan "$D")" in *"/Users/tmp/keep"*) v=ok;; *) v=miss;; esac
check "real /Users/tmp/ path not masked by the /tmp/ fixture rule" ok "$v"

# e) hunk line numbers are tracked (path on the 3rd added line of a hunk starting at +10 → 12)
D=$'+++ b/h.js\n@@ -1,0 +10,3 @@\n+ok1\n+ok2\n+p = "/Users/x/y"'
case "$(scan "$D")" in "h.js:12: "*) v=ok;; *) v=miss;; esac
check "hunk line number reported (start +10, 3rd added line = 12)" ok "$v"

# f) FIX (qingyun-wu): *-prefixed real paths flag — a C-pointer deref and a markdown
#    bullet were silently skipped by the bare-`*` comment rule.
D=$'+++ b/p.c\n@@ -1,0 +1,2 @@\n+*cfg = "/Users/bob/.ssh/id_rsa";\n+* see /Users/bob/notes'
n="$(scan "$D" | grep -c '/Users/bob')"
check "*-prefixed real paths flagged (not skipped as comments)" 2 "$n"

# g) FIX (qingyun-wu): a filename containing a space keeps the correct file:line
#    label — $2 split it at the space and reported the wrong file.
D=$'+++ b/my file.py\n@@ -1,0 +1,1 @@\n+home = "/Users/alice/app"'
case "$(scan "$D")" in "my file.py:1: "*) v=ok;; *) v=miss;; esac
check "spaced filename preserved in file:line label" ok "$v"

[ "$fail" -eq 0 ] && echo PASS || echo FAILED
exit $fail
