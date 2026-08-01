#!/usr/bin/env bash
# The `--ours` conflict fallback must not destroy the incoming version silently.
#
# sync-workspace resolves an unresolvable merge conflict by keeping the merging
# host's file (`git checkout --ours`). That is right for host-local state and
# wrong for anything both hosts append to: on 2026-07-31 it dropped two
# MEMORY.md index lines (merge 64dec1b2) and a WIRE episode-index entry
# (merge 258c349b), and the second stayed missing for ~2 days. Neither surfaced
# — the log named the peer but never the files, and `git log -- FILE` cannot
# show a change destroyed IN a merge because history simplification hides merge
# commits.
#
# This drives the REAL fallback block over a real two-branch conflict and
# asserts the discarded side is recoverable afterwards.
#
# Run: bash tests/sync-workspace-conflict-preserves-theirs.test.sh
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/scripts/sync-workspace.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Load ONLY the function under test, by extracting its definition from the real
# script. The script ends in an unguarded dispatch, so it cannot simply be
# sourced; slicing the definition keeps this a test OF the script rather than a
# copy of it. `log` and `color_warn` are the script's helpers — stub them.
log() { :; }
color_warn() { printf 'WARN %s\n' "$*"; }
eval "$(awk '/^_resolve_conflicts_keep_ours\(\) \{/,/^\}/' "$SCRIPT")"
if ! declare -F _resolve_conflicts_keep_ours >/dev/null; then
    printf '  FAIL could not load _resolve_conflicts_keep_ours from %s\n' "$SCRIPT"
    printf '\nsync conflict preserves theirs: 0 passed, 1 failed\n'
    exit 1
fi
ok "loaded _resolve_conflicts_keep_ours from the real script"

# --- a real conflict, resolved by the REAL function --------------------------
WS="$TMP/ws"; mkdir -p "$WS"; cd "$WS" || exit 1
git init -q .; git config user.email t@t; git config user.name t
printf 'line1\nline2\n' > index.md
git add index.md; git commit -qm base
git checkout -q -b peer
printf 'line1\nline2\nPEER-ONLY-ENTRY\n' > index.md
git commit -qam peer
git checkout -q -; printf 'line1\nline2\nOURS-ONLY-ENTRY\n' > index.md
git commit -qam ours
git merge peer >/dev/null 2>&1
[ "$(git diff --name-only --diff-filter=U)" = "index.md" ]
check $? "fixture really produces a conflict on index.md"

BK="$WS/backup"
_resolve_conflicts_keep_ours "origin/peer" "$BK"

[ -z "$(git diff --name-only --diff-filter=U)" ]
check $? "function leaves NO unmerged paths (merge can conclude)"
grep -q OURS-ONLY-ENTRY index.md; check $? "our side is kept (unchanged behaviour)"
! grep -q PEER-ONLY-ENTRY index.md; check $? "peer's line is absent from the result, as before"
grep -q PEER-ONLY-ENTRY "$BK/index.md" 2>/dev/null
check $? "THE POINT: the discarded peer version is recoverable from the backup"

# --- a DD conflict has no stage-3 blob: must skip, not crash -----------------
git checkout -q -b dd1; git rm -q index.md; git commit -qm "delete ours"
git checkout -q -b dd2 HEAD~1; git rm -q index.md; git commit -qm "delete theirs"
git checkout -q dd1; git merge dd2 >/dev/null 2>&1
_resolve_conflicts_keep_ours "origin/dd2" "$TMP/bk2" 2>/dev/null
[ -z "$(git diff --name-only --diff-filter=U)" ]
check $? "DD conflict (no stage 3) resolves without crashing"

printf '\n%s: %d passed, %d failed\n' "sync conflict preserves theirs" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
printf 'PASS — sync-workspace conflict fallback preserves the discarded side\n'
