#!/usr/bin/env bash
# SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 omits the ~/Desktop transcript hook and
# nothing else. Three arms, because "did not install it" alone would also pass if
# the installer wrote no PreCompact hooks at all.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
pass=0; fail=0

arm() {
    local optout="$1" t
    t="$(mktemp -d -t hooks-optout.XXXXXX)"
    mkdir -p "$t/src" "$t/skills" "$t/.claude"
    cp "$REPO/src/install-claude-hooks.sh" "$REPO/src/skill_hooks.py" "$t/src/"
    : > "$t/src/session-handoff.sh"; : > "$t/src/check-pending-tasks.sh"
    echo '{}' > "$t/.claude/settings.json"
    if [ -n "$optout" ]; then
        SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE="$optout" bash "$t/src/install-claude-hooks.sh" >/dev/null 2>&1
    else
        bash "$t/src/install-claude-hooks.sh" >/dev/null 2>&1
    fi
    python3 - "$t/.claude/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
c = [h.get("command", "") for g in d.get("hooks", {}).get("PreCompact", [])
     for h in g.get("hooks", [])]
print(sum("sutando-conversations/" in x for x in c), len(c))
PY
    rm -rf "$t"
}

check() {
    if [ "$2" = "$3" ]; then echo "  ok   $1"; pass=$((pass+1))
    else echo "  FAIL $1 — expected '$3', got '$2'"; fail=$((fail+1)); fi
}

check "unset: archiver installed alongside session-handoff (unchanged default)" "$(arm '')"  "1 2"
check "=1: archiver omitted, session-handoff still installed"                   "$(arm 1)"  "0 1"
check "=0: only the literal 1 opts out"                                         "$(arm 0)"  "1 2"

echo
if [ "$fail" -gt 0 ]; then echo "transcript-archive opt-out: $fail failure(s), $pass passed"; exit 1; fi
echo "transcript-archive opt-out: all $pass checks passed"
