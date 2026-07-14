#!/usr/bin/env bash
# Tests for src/schedule-crons-session-hint.sh and
# scripts/install-session-start-hook.sh.
#
# Covers:
#   1. Hint script outputs valid JSON with expected structure
#   2. Installer creates settings.json from scratch
#   3. Installer adds SessionStart hook to existing settings with no SessionStart key
#   4. Installer is idempotent (re-running with hook present skips cleanly)
#   5. Installer preserves other hook keys (PreCompact, SessionEnd, Stop)
#   6. Installer adds SessionStart hook to settings.json with existing SessionStart entries
#
# Run: bash tests/session-start-schedule-crons-hook.test.sh
# Exit: 0 = all pass, non-zero = failure

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
HINT="$REPO/src/schedule-crons-session-hint.sh"
INSTALLER="$REPO/scripts/install-session-start-hook.sh"

TMPDIR_BASE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

pass=0; fail=0
ok()   { echo "  ok  $1"; pass=$((pass+1)); }
fail() { echo "FAIL: $1" >&2; fail=$((fail+1)); }

# ── 1. Hint script outputs valid JSON with expected structure ──────────────────
# The hint gates on SUTANDO_CORE_SESSION=1 (only the core bootstraps), so set it
# for the "fires" assertions below.
out="$(SUTANDO_CORE_SESSION=1 bash "$HINT")"
# Must be valid JSON
echo "$out" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
  && ok "hint script outputs valid JSON" \
  || fail "hint script output is not valid JSON: $out"

# Must contain hookSpecificOutput.additionalContext
ctx="$(echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d['hookSpecificOutput']['additionalContext'])
" 2>/dev/null)"
[ -n "$ctx" ] \
  && ok "hint script additionalContext is non-empty" \
  || fail "hint script missing hookSpecificOutput.additionalContext"

# Must mention /startup (the canonical bootstrap: orphan-recovery THEN crons)
case "$ctx" in
  */startup*) ok "hint script mentions /startup" ;;
  *) fail "hint script additionalContext does not mention /startup: $ctx" ;;
esac

# Scope gate: WITHOUT the core marker the hint must stay silent (no context) so
# ad-hoc sessions in the checkout don't trigger a redundant cron bootstrap.
out_nogate="$(env -u SUTANDO_CORE_SESSION bash "$HINT")"
[ -z "$out_nogate" ] \
  && ok "hint script is silent without SUTANDO_CORE_SESSION marker" \
  || fail "hint script emitted output without the core marker: $out_nogate"

# ── 2. Installer creates settings.json from scratch ───────────────────────────
# NOTE: do NOT invoke the real installer here — it hardcodes REPO and would
# mutate this checkout's live .claude/settings.json, dirtying the worktree.
# We replay the installer's Python merge logic against a temp settings.json
# instead (the installer's own logic is unit-tested in the .test.py sibling).
T="$TMPDIR_BASE/fresh"
mkdir -p "$T/.claude"

# Install on a temp settings.json by calling the Python block directly
python3 - "$T/.claude/settings.json" "bash \"$HINT\"" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
if not os.path.exists(settings_path):
    with open(settings_path, "w") as f:
        json.dump({"hooks": {}}, f)

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

for entry in session_start:
    for h in entry.get("hooks", []):
        if h.get("command", "") == hook_cmd:
            print("already installed")
            sys.exit(0)

session_start.insert(0, {
    "matcher": "",
    "hooks": [{"type": "command", "command": hook_cmd}]
})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("installed")
PYEOF

result="$(python3 -c "
import json
with open('$T/.claude/settings.json') as f: d=json.load(f)
ss=d['hooks'].get('SessionStart',[])
cmds=[h['command'] for e in ss for h in e.get('hooks',[])]
print(cmds)
" 2>/dev/null)"
case "$result" in
  *"$HINT"*) ok "installer creates settings.json with SessionStart hook" ;;
  *) fail "installer did not add hook to fresh settings.json: $result" ;;
esac

# ── 3. Adding to existing settings that has other hooks (no SessionStart) ──────
T2="$TMPDIR_BASE/existing"
mkdir -p "$T2/.claude"
cat > "$T2/.claude/settings.json" <<'JSONEOF'
{
  "hooks": {
    "PreCompact": [{"matcher":"","hooks":[{"type":"command","command":"echo precompact"}]}],
    "Stop": [{"matcher":"","hooks":[{"type":"command","command":"echo stop"}]}]
  }
}
JSONEOF
python3 - "$T2/.claude/settings.json" "bash \"$HINT\"" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

for entry in session_start:
    for h in entry.get("hooks", []):
        if h.get("command", "") == hook_cmd:
            print("already installed")
            sys.exit(0)

session_start.insert(0, {
    "matcher": "",
    "hooks": [{"type": "command", "command": hook_cmd}]
})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("installed")
PYEOF

result2="$(python3 -c "
import json
with open('$T2/.claude/settings.json') as f: d=json.load(f)
ss=d['hooks'].get('SessionStart',[])
cmds=[h['command'] for e in ss for h in e.get('hooks',[])]
has_precompact='PreCompact' in d['hooks']
has_stop='Stop' in d['hooks']
print(cmds, has_precompact, has_stop)
" 2>/dev/null)"
case "$result2" in
  *"$HINT"*True*True*) ok "installer adds SessionStart and preserves PreCompact + Stop" ;;
  *) fail "installer did not preserve other hooks: $result2" ;;
esac

# ── 4. Idempotency — re-running with hook present skips cleanly ───────────────
mtime_before="$(python3 -c "import os; print(os.path.getmtime('$T2/.claude/settings.json'))")"
out_idem="$(python3 - "$T2/.claude/settings.json" "bash \"$HINT\"" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

for entry in session_start:
    for h in entry.get("hooks", []):
        if h.get("command", "") == hook_cmd:
            print("already installed")
            sys.exit(0)

session_start.insert(0, {
    "matcher": "",
    "hooks": [{"type": "command", "command": hook_cmd}]
})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("installed")
PYEOF
)"
[ "$out_idem" = "already installed" ] \
  && ok "installer is idempotent (skips when hook already present)" \
  || fail "installer re-installed an already-present hook (output: $out_idem)"

# Count SessionStart entries — must still be exactly 1 after idempotent run
count="$(python3 -c "
import json
with open('$T2/.claude/settings.json') as f: d=json.load(f)
ss=d['hooks'].get('SessionStart',[])
print(len(ss))
" 2>/dev/null)"
[ "$count" = "1" ] \
  && ok "idempotent run does not duplicate SessionStart entries" \
  || fail "expected 1 SessionStart entry, got $count"

# ── 5. Installer adds alongside existing SessionStart entries (different cmd) ──
T3="$TMPDIR_BASE/withother"
mkdir -p "$T3/.claude"
cat > "$T3/.claude/settings.json" <<'JSONEOF'
{
  "hooks": {
    "SessionStart": [{"matcher":"","hooks":[{"type":"command","command":"echo other-hook"}]}]
  }
}
JSONEOF
python3 - "$T3/.claude/settings.json" "bash \"$HINT\"" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

for entry in session_start:
    for h in entry.get("hooks", []):
        if h.get("command", "") == hook_cmd:
            print("already installed")
            sys.exit(0)

session_start.insert(0, {
    "matcher": "",
    "hooks": [{"type": "command", "command": hook_cmd}]
})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("installed")
PYEOF

count3="$(python3 -c "
import json
with open('$T3/.claude/settings.json') as f: d=json.load(f)
ss=d['hooks'].get('SessionStart',[])
print(len(ss))
" 2>/dev/null)"
[ "$count3" = "2" ] \
  && ok "installer adds alongside existing different-command SessionStart entry" \
  || fail "expected 2 SessionStart entries (ours + existing), got $count3"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
