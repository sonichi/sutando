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
# #4309 round 16 (keweichen): the retry must replay the original attempt's
# archive-consent decision, and a repeat failure must abort (not warn+continue)
# per this file's own documented fail-closed invariant.
grep -qF 'archive_opted_in=' "$REPO/scripts/sutando-migrate.sh" \
    || { echo "  FAIL: archive_opted_in= missing from scripts/sutando-migrate.sh (marker no longer persists consent)"; FAILURES=$((FAILURES + 1)); }
grep -qF '_retry_archive_opted_in' "$REPO/src/startup.sh" \
    || { echo "  FAIL: _retry_archive_opted_in missing from src/startup.sh (retry doesn't read persisted consent)"; FAILURES=$((FAILURES + 1)); }
grep -A3 'hook bridge retry failed again' "$REPO/src/startup.sh" | grep -qF 'exit 1' \
    || { echo "  FAIL: startup.sh's repeat-retry-failure branch no longer aborts (exit 1)"; FAILURES=$((FAILURES + 1)); }

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
check "boot 1: marker persists archive_opted_in=0 (malformed settings had no prior archive hook -- never opted in)" \
    "$(grep -qxF 'archive_opted_in=0' "$PC/ws/state/.hook-bridge-retry-needed" && echo 0 || echo 1)" \
    "$(cat "$PC/ws/state/.hook-bridge-retry-needed" 2>/dev/null)"

# ── Boot 2, attempt A: startup.sh's retry-consult snippet, settings STILL malformed.
# Extracted (not sourcing all of startup.sh, which starts real servers) --
# same convention as tests/startup-re-migrate-loop.test.sh. Mirrors the
# post-round-16 logic: replay the persisted archive_opted_in, and a repeat
# failure aborts (exit 1) rather than warning and continuing.
run_retry_snippet() {
    HOME="$PC/home" bash -c '
        _ws_new="'"$PC"'/ws"
        _hook_bridge_retry_marker="${_ws_new}/state/.hook-bridge-retry-needed"
        if [ -n "$_ws_new" ] && [ -f "$_hook_bridge_retry_marker" ]; then
            _retry_archive_opted_in="$(sed -n "s/^archive_opted_in=//p" "$_hook_bridge_retry_marker" 2>/dev/null | tail -1)"
            if [ "$_retry_archive_opted_in" = "1" ]; then
                _retry_rc=0; bash "'"$PC"'/repo/src/install-claude-hooks.sh" >/dev/null 2>&1 || _retry_rc=$?
            else
                _retry_rc=0; SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 bash "'"$PC"'/repo/src/install-claude-hooks.sh" >/dev/null 2>&1 || _retry_rc=$?
            fi
            if [ "$_retry_rc" -eq 0 ]; then
                rm -f "$_hook_bridge_retry_marker"
                echo "RETRY-OK"
            else
                echo "RETRY-ABORT"
                exit 1
            fi
        else
            echo "NO-MARKER"
        fi
    '
}

RETRY2_RC=0
RETRY2="$(run_retry_snippet)" || RETRY2_RC=$?
check "boot 2 (still-broken settings): retry attempted and failed again" \
    "$([ "$RETRY2" = "RETRY-ABORT" ] && echo 0 || echo 1)" "got: $RETRY2"
check "boot 2 (still-broken settings): a repeat failure aborts (nonzero exit) rather than warn-and-continue" \
    "$([ "$RETRY2_RC" -ne 0 ] && echo 0 || echo 1)" "got rc=$RETRY2_RC"
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
check "THE POINT: retry replayed archive_opted_in=0 -- archive hook NOT added on the retried settings file" \
    "$(jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("archive-transcript.sh"))] | length == 0' "$CORE_CCD/settings.json" >/dev/null 2>&1 && echo 0 || echo 1)"

printf '\n%s\n' "$OUT1" | tail -6

# ── Second fixture: the operator had ALREADY opted in to archiving (a
# matching hook exists in the legacy $HOME/.claude/settings.json), and the
# NEW target settings.json is separately malformed. _archive_opted_in must
# read 1 from that prior opt-in, persist it, and the retry must replay it --
# i.e. the archive hook DOES get added once the bridge is retried.
PC2="$(mktemp -d -t migrate-retry-marker2-XXXXXX)"
trap 'rm -rf "$PC" "$PC2"' EXIT
mkdir -p "$PC2/repo/scripts" "$PC2/repo/src" "$PC2/ws/state" "$PC2/home" \
         "$PC2/src/a" "$PC2/src/b" "$PC2/src/c" "$PC2/home/.claude"
cp "$REPO/scripts/sutando-migrate.sh"       "$PC2/repo/scripts/"
cp "$REPO/scripts/sutando-config.sh"        "$PC2/repo/scripts/"
cp "$REPO/scripts/sutando-config-hooks.sh"  "$PC2/repo/scripts/"
cp "$REPO/scripts/python-binary.sh"         "$PC2/repo/scripts/"
cp "$REPO/src/sutando_config.py"            "$PC2/repo/src/"
cp "$REPO/src/install-claude-hooks.sh"      "$PC2/repo/src/"
cp "$REPO/sutando.config.json"              "$PC2/repo/"
touch "$PC2/repo/CLAUDE.md" \
      "$PC2/repo/src/archive-transcript.sh" "$PC2/repo/src/session-handoff.sh" \
      "$PC2/repo/src/check-pending-tasks.sh" "$PC2/repo/src/turn-start.sh"
printf '{"workspace": {"path": "%s"}}\n' "$PC2/ws" > "$PC2/repo/sutando.config.local.json"
mkdir -p "$PC2/src/b/notes"; echo "content" > "$PC2/src/b/notes/keep.md"

# Legacy settings already carry the real archiver shape this installer emits
# (same command install-claude-hooks.sh writes -- verified against a live
# run at round 16). This is the "already opted in" signal. Resolve each half
# the SAME way the real script does: REPO_DIR via bash `cd .. && pwd`
# (never /private-resolved), WORKSPACE_DIR via sutando-config.sh's own
# Python resolver (which IS /private-resolved on macOS) -- using bash `pwd`
# for both, as a naive fixture would, produces a command that could never
# match a genuine prior write.
_pc2_repo_real="$(cd "$PC2/repo" && pwd)"
_pc2_ws_real="$(HOME="$PC2/home" bash "$PC2/repo/scripts/sutando-config.sh" workspace 2>/dev/null)"
_pc2_archive_cmd="bash '$_pc2_repo_real/src/archive-transcript.sh' '$_pc2_ws_real/logs/conversations/'"
printf '{"hooks":{"PreCompact":[{"hooks":[{"type":"command","command":%s}]}]}}\n' \
    "$(printf '%s' "$_pc2_archive_cmd" | jq -Rs .)" > "$PC2/home/.claude/settings.json"

CORE_CCD2="$(HOME="$PC2/home" bash "$PC2/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
mkdir -p "$CORE_CCD2"
printf '{not json' > "$CORE_CCD2/settings.json"   # target is malformed -> bridge fails

set +e
HOME="$PC2/home" \
    SUTANDO_MIGRATE_SRC_A="$PC2/src/a" SUTANDO_MIGRATE_SRC_B="$PC2/src/b" SUTANDO_MIGRATE_SRC_C="$PC2/src/c" \
    bash "$PC2/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import >/dev/null 2>&1
set -e

check "opted-in fixture: marker persists archive_opted_in=1 (legacy settings already had the archive hook)" \
    "$(grep -qxF 'archive_opted_in=1' "$PC2/ws/state/.hook-bridge-retry-needed" 2>/dev/null && echo 0 || echo 1)" \
    "$(cat "$PC2/ws/state/.hook-bridge-retry-needed" 2>/dev/null)"

echo '{"hooks":{}}' > "$CORE_CCD2/settings.json"   # operator fixes the malformed file
run_retry_snippet2() {
    HOME="$PC2/home" bash -c '
        _ws_new="'"$PC2"'/ws"
        _hook_bridge_retry_marker="${_ws_new}/state/.hook-bridge-retry-needed"
        if [ -n "$_ws_new" ] && [ -f "$_hook_bridge_retry_marker" ]; then
            _retry_archive_opted_in="$(sed -n "s/^archive_opted_in=//p" "$_hook_bridge_retry_marker" 2>/dev/null | tail -1)"
            if [ "$_retry_archive_opted_in" = "1" ]; then
                _retry_rc=0; bash "'"$PC2"'/repo/src/install-claude-hooks.sh" >/dev/null 2>&1 || _retry_rc=$?
            else
                _retry_rc=0; SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 bash "'"$PC2"'/repo/src/install-claude-hooks.sh" >/dev/null 2>&1 || _retry_rc=$?
            fi
            if [ "$_retry_rc" -eq 0 ]; then rm -f "$_hook_bridge_retry_marker"; echo "RETRY-OK"; else echo "RETRY-ABORT"; exit 1; fi
        else
            echo "NO-MARKER"
        fi
    '
}
RETRY_PC2="$(run_retry_snippet2)"
check "opted-in fixture: retry succeeds once the malformed file is fixed" \
    "$([ "$RETRY_PC2" = "RETRY-OK" ] && echo 0 || echo 1)" "got: $RETRY_PC2"
check "THE POINT (opted-in): retry replayed archive_opted_in=1 -- archive hook IS added on the retried settings file" \
    "$(jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("archive-transcript.sh"))] | length >= 1' "$CORE_CCD2/settings.json" >/dev/null 2>&1 && echo 0 || echo 1)"

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All hook-bridge retry-marker checks passed."
