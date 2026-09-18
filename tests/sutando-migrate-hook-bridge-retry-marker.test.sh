#!/bin/bash
# #4309 review round 15 (qingyun-wu/keweichen): a per-source migration
# sentinel means the file copy succeeded, not that the hook bridge did --
# `sutando-migrate.sh` writes source sentinels BEFORE the bridge runs, then
# writes `.hook-bridge-retry-needed` + returns 1 on bridge failure. But
# `src/startup.sh:382-389` treated ANY source sentinel as "already migrated"
# and skipped the whole block on the next boot -- so a failed bridge was
# never retried, and the prior failure tests used empty sources, never
# exercising a real nonempty-source two-boot sequence.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

# Structural tie-back: the snippet exercised below (run_retry_snippet) is a
# hand-extracted copy of startup.sh's own logic (same convention as
# tests/startup-re-migrate-loop.test.sh) -- assert the real file still
# defines the variable this test's snippet stands in for, so a later edit
# to startup.sh that drops the check can't pass silently against a stale copy.
grep -qF '_hook_bridge_retry_marker' "$REPO/src/startup.sh" \
    || { echo "  FAIL: _hook_bridge_retry_marker missing from src/startup.sh"; FAILURES=$((FAILURES + 1)); }
grep -qF '.hook-bridge-retry-needed' "$REPO/src/startup.sh" \
    || { echo "  FAIL: retry-marker filename missing from src/startup.sh"; FAILURES=$((FAILURES + 1)); }

PC="$(mktemp -d -t migrate-retry-marker-XXXXXX)"
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

# A REAL, nonempty recognized source -- SUTANDO_MIGRATE_SRC_B stands in for
# the legacy pre-v0.8 workspace dir startup.sh's own env-migration path
# (`_ws_legacy`) copies from. The prior failure tests left every source
# empty and so never exercised a copy actually landing.
mkdir -p "$PC/src/b/notes"
echo "real legacy note content" > "$PC/src/b/notes/keep.md"

CORE_CCD="$(HOME="$PC/home" bash "$PC/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
mkdir -p "$CORE_CCD"
# Malformed target settings.json -> install-claude-hooks.sh's jq edit fails.
printf '{not json' > "$CORE_CCD/settings.json"

echo "sutando-migrate + startup: two-boot hook-bridge-retry-marker interaction"

# ── Boot 1: commit runs, bridge fails (malformed settings), retry marker written.
set +e
OUT1="$(HOME="$PC/home" \
        SUTANDO_MIGRATE_SRC_A="$PC/src/a" \
        SUTANDO_MIGRATE_SRC_B="$PC/src/b" \
        SUTANDO_MIGRATE_SRC_C="$PC/src/c" \
        bash "$PC/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"
RC1=$?
set -e

check "boot 1: nonempty source actually copied (fixture sanity)" \
    "$([ -f "$PC/ws/notes/keep.md" ] && echo 0 || echo 1)" "notes/keep.md missing from $PC/ws"
check "boot 1: --commit reports failure (bridge broke)" \
    "$([ "$RC1" -ne 0 ] && echo 0 || echo 1)" "got rc=$RC1"
check "boot 1: per-source sentinel present despite bridge failure" \
    "$(ls "$PC/ws"/state/.migrated-from-* >/dev/null 2>&1 && echo 0 || echo 1)"
check "boot 1: retry marker present" \
    "$([ -f "$PC/ws/state/.hook-bridge-retry-needed" ] && echo 0 || echo 1)"

# ── Boot 2, attempt A: startup.sh's retry-consult snippet, settings STILL malformed.
# Extracted (not sourcing all of startup.sh, which starts real servers) --
# same convention as tests/startup-re-migrate-loop.test.sh.
run_retry_snippet() {
    HOME="$PC/home" bash -c '
        _ws_new="'"$PC"'/ws"
        _hook_bridge_retry_marker="${_ws_new}/state/.hook-bridge-retry-needed"
        if [ -n "$_ws_new" ] && [ -f "$_hook_bridge_retry_marker" ]; then
            if bash "'"$PC"'/repo/src/install-claude-hooks.sh" >/dev/null 2>&1; then
                rm -f "$_hook_bridge_retry_marker"
                echo "RETRY-OK"
            else
                echo "RETRY-FAILED"
            fi
        else
            echo "NO-MARKER"
        fi
    '
}

RETRY2="$(run_retry_snippet)"
check "boot 2 (still-broken settings): retry attempted and failed again" \
    "$([ "$RETRY2" = "RETRY-FAILED" ] && echo 0 || echo 1)" "got: $RETRY2"
check "boot 2 (still-broken settings): marker NOT cleared (nothing to retry against would be true failure to leave it)" \
    "$([ -f "$PC/ws/state/.hook-bridge-retry-needed" ] && echo 0 || echo 1)"

# ── Boot 2, attempt B: operator fixes the settings file; retry now succeeds.
echo '{"hooks":{}}' > "$CORE_CCD/settings.json"
RETRY3="$(run_retry_snippet)"
check "THE POINT: retry succeeds once the underlying breakage is fixed" \
    "$([ "$RETRY3" = "RETRY-OK" ] && echo 0 || echo 1)" "got: $RETRY3"
check "THE POINT: retry marker is cleared only after a successful bridge" \
    "$([ ! -f "$PC/ws/state/.hook-bridge-retry-needed" ] && echo 0 || echo 1)" \
    "marker survived a successful retry"
check "THE POINT: core hooks actually landed on the retried settings file" \
    "$(jq -e '[.hooks.Stop // [] | .[].hooks[] | select(.command | contains("check-pending-tasks.sh"))] | length >= 1' "$CORE_CCD/settings.json" >/dev/null 2>&1 && echo 0 || echo 1)"

printf '\n%s\n' "$OUT1" | tail -6

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All hook-bridge retry-marker checks passed."
