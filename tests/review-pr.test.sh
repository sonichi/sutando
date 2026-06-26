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

[ "$fail" -eq 0 ] && echo PASS || echo FAILED
exit $fail
