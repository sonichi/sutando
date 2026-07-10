#!/usr/bin/env bash
# Tests for scripts/lint-sutando-home-path.sh — the ~/.sutando/ install-path guard.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
LINT="scripts/lint-sutando-home-path.sh"
fail=0
ok()   { echo "ok   - $1"; }
bad()  { echo "FAIL - $1"; fail=1; }

# The pattern the lint uses (kept in sync with the script).
PATTERN='(\$HOME|~)/\.sutando/|home\(\)[[:space:]]*/[[:space:]]*["'\'']\.sutando'

matches() { printf '%s\n' "$1" | grep -Eq "$PATTERN"; }

# 1. The current tree is clean in all-mode (every existing ~/.sutando use is a
#    resolver-owner / intentional fallback in the ALLOWED list).
if bash "$LINT" >/dev/null 2>&1; then ok "all-mode passes on the current tree"; else bad "all-mode passes on the current tree"; fi

# 2. The pattern CATCHES the real offending forms.
if matches 'WS="$HOME/.sutando/newthing"';           then ok "catches shell \$HOME/.sutando/ literal"; else bad "catches shell \$HOME/.sutando/ literal"; fi
if matches 'x=~/.sutando/repo/x';                      then ok "catches shell ~/.sutando/ literal";      else bad "catches shell ~/.sutando/ literal"; fi
if matches 'p = Path.home() / ".sutando" / "repo"';    then ok "catches python Path.home() / .sutando";  else bad "catches python Path.home() / .sutando"; fi

# 3. The pattern does NOT catch unrelated / bare mentions.
if matches 'd=~/.claude/skills';                       then bad "ignores a bare ~/.claude path";          else ok "ignores a bare ~/.claude path"; fi
if matches 'echo .sutando-memory-sync';                then bad "ignores .sutando-memory-sync dir name";  else ok "ignores .sutando-memory-sync dir name"; fi

# 4. The lint self-whitelists (its own file carries the literal in examples).
if grep -q 'lint-sutando-home-path\\.sh' "$LINT";      then ok "lint file itself is in the ALLOWED list"; else bad "lint file itself is in the ALLOWED list"; fi

if [[ "$fail" -eq 0 ]]; then echo "PASS"; else echo "SOME TESTS FAILED"; exit 1; fi
