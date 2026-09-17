#!/bin/bash
# The hook bridge in `_commit_hooks_channels_bridges()` only ever calls
# `sutando-config-hooks.sh install <settings> --with-catchup-hook`, so a
# migrated core gets ONLY the SessionEnd catchup entry. #4309's own move put
# THREE more core-only hooks (PreCompact archiver, PreCompact handoff, Stop)
# and a legacy-project-settings sweep into `src/install-claude-hooks.sh`,
# targeting the SAME `claude-sutando-config-dir` the bridge resolves for
# `_new_settings` -- but the bridge never calls it, so a freshly-migrated
# core is missing 3 of its 4 core-only hooks and any pre-move legacy-project
# entries never get swept (qingyun-wu, round 9, exact head 8d603b4d6).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

# Same isolated-repo-skeleton pattern as
# sutando-migrate-test-hook-covers-config-dir.test.sh's positive control:
# own repo, workspace, HOME and SRC_{A,B,C}, so this never touches real
# operator config. Adds src/install-claude-hooks.sh + the file targets its
# HOOKS array names (empty stubs -- install writes command STRINGS, it never
# executes them) so the bridge under test has a real installer to delegate to.
PC="$(mktemp -d -t migrate-hook-delegation-XXXXXX)"
trap 'rm -rf "$PC"' EXIT
mkdir -p "$PC/repo/scripts" "$PC/repo/src" "$PC/ws/state" "$PC/home" \
         "$PC/src/a" "$PC/src/b" "$PC/src/c"
cp "$REPO/scripts/sutando-migrate.sh"       "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config.sh"        "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config-hooks.sh"  "$PC/repo/scripts/"
cp "$REPO/scripts/python-binary.sh"         "$PC/repo/scripts/"
cp "$REPO/src/sutando_config.py"            "$PC/repo/src/"
cp "$REPO/src/install-claude-hooks.sh"      "$PC/repo/src/"
cp "$REPO/sutando.config.json"              "$PC/repo/"
touch "$PC/repo/CLAUDE.md" \
      "$PC/repo/src/archive-transcript.sh" "$PC/repo/src/session-handoff.sh" \
      "$PC/repo/src/check-pending-tasks.sh" "$PC/repo/src/turn-start.sh"
printf '{"workspace": {"path": "%s"}}\n' "$PC/ws" > "$PC/repo/sutando.config.local.json"

# A pre-existing PROJECT-level settings.json carrying the deprecated
# ~/Desktop archiver shape -- the "legacy project-scope sweep" qingyun-wu
# named. install-claude-hooks.sh sweeps this regardless of the omit flag;
# the old catchup-only bridge call never touches it at all.
mkdir -p "$PC/repo/.claude"
cat > "$PC/repo/.claude/settings.json" <<JSON
{"hooks":{"PreCompact":[{"hooks":[{"type":"command","command":"cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\""}]}]}}
JSON

echo "sutando-migrate: hook bridge delegates to the primary installer"

OUT="$(HOME="$PC/home" \
       SUTANDO_MIGRATE_SRC_A="$PC/src/a" \
       SUTANDO_MIGRATE_SRC_B="$PC/src/b" \
       SUTANDO_MIGRATE_SRC_C="$PC/src/c" \
       bash "$PC/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"

CORE_SETTINGS="$(HOME="$PC/home" bash "$PC/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)/settings.json"

[ -f "$CORE_SETTINGS" ]
check "core settings.json exists after migration" $? "not found at $CORE_SETTINGS"

# THE POINT: all 4 core-only hooks landed, not just the catchup one.
jq -e '[.hooks.SessionEnd // [] | .[].hooks[] | select(.command | contains("session-handoff.sh"))] | length >= 1' \
   "$CORE_SETTINGS" >/dev/null 2>&1
check "SessionEnd catchup hook present (already worked pre-fix)" $?

jq -e '[.hooks.Stop // [] | .[].hooks[] | select(.command | contains("check-pending-tasks.sh"))] | length >= 1' \
   "$CORE_SETTINGS" >/dev/null 2>&1
check "THE POINT: Stop hook (check-pending-tasks.sh) present -- primary installer ran" $? \
    "missing -- the bridge never delegated to install-claude-hooks.sh"

jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("session-handoff.sh"))] | length >= 1' \
   "$CORE_SETTINGS" >/dev/null 2>&1
check "THE POINT: PreCompact handoff hook present" $? "missing -- primary installer never ran"

jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("archive-transcript.sh"))] | length >= 1' \
   "$CORE_SETTINGS" >/dev/null 2>&1
check "THE POINT: PreCompact archiver hook present" $? "missing -- primary installer never ran"

# THE POINT: the legacy PROJECT-level deprecated shape got swept too.
! jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("Desktop/sutando-conversations"))] | length >= 1' \
   "$PC/repo/.claude/settings.json" >/dev/null 2>&1
check "THE POINT: legacy project-scope sweep ran (deprecated ~/Desktop shape removed)" $? \
    "the deprecated project-level entry survived -- no legacy sweep happened"

grep -q "primary installer" <<<"$OUT"
check "bridge output names the primary-installer delegation" $?

printf '\n%s\n' "$OUT" | tail -5

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All hook-bridge delegation checks passed."
