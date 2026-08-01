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

# --- BLOCKER 1 regression: a backup WRITE FAILURE must be loud and must not
# --- be summarised as success (john-the-dev #2476). Force it the way the
# --- reviewer did: make the backup's parent dir a regular FILE.
WS2="$TMP/ws2"; mkdir -p "$WS2"; cd "$WS2" || exit 1
git init -q .; git config user.email t@t; git config user.name t
mkdir -p dir; printf 'base\n' > dir/note.md
git add dir/note.md; git commit -qm base
git checkout -q -b peer2
printf 'base\nPEER-ONLY\n' > dir/note.md; git commit -qam peer
git checkout -q -; printf 'base\nOURS-ONLY\n' > dir/note.md; git commit -qam ours
git merge peer2 >/dev/null 2>&1
BK2="$WS2/backup"
mkdir -p "$BK2"; : > "$BK2/dir"          # a FILE where mkdir -p needs a dir
OUT="$(_resolve_conflicts_keep_ours "origin/peer2" "$BK2" 2>&1)"

[ -z "$(git diff --name-only --diff-filter=U)" ]
check $? "write-failure: merge still concludes (preservation stays non-blocking)"
grep -q OURS-ONLY dir/note.md; check $? "write-failure: our side still kept"
printf '%s' "$OUT" | grep -q "could NOT preserve"
check $? "write-failure: the failing path is named LOUDLY"
printf '%s' "$OUT" | grep -q "NOT saved"
check $? "write-failure: summary reports the failure"
# Match EVERY known phrasing of the total-preservation claim, including the
# pre-fix one — greping only the new wording passes vacuously against the old
# code, which is the defect this whole PR is about.
printf '%s' "$OUT" | grep -qE "each discarded incoming file is preserved|all [0-9]+ discarded incoming file"
[ $? -ne 0 ]; check $? "write-failure: summary does NOT claim everything was preserved (any wording)"

# --- BLOCKER 2 regression: the DEFAULT backup location is git-private, so no
# --- vault.sync.include configuration can stage it.
cd "$WS2" || exit 1
git checkout -q -b peer3 2>/dev/null; printf 'x\nP3\n' > dir/note.md; git commit -qam p3 >/dev/null 2>&1
git checkout -q -; printf 'x\nO3\n' > dir/note.md; git commit -qam o3 >/dev/null 2>&1
git merge peer3 >/dev/null 2>&1
_resolve_conflicts_keep_ours "origin/peer3" >/dev/null 2>&1     # NO explicit root -> default
GITDIR="$(git rev-parse --git-dir)"
[ -d "$GITDIR/sutando-sync-conflicts" ]
check $? "default backup root lives under the git dir (never trackable)"
# (Removed a "cannot appear in git status" assertion here: against the pre-fix
# code it passed for the wrong reason — that default root sat OUTSIDE the repo,
# so git status was trivially clean. The under-the-git-dir check above is the
# one that actually discriminates.)

printf '\n%s: %d passed, %d failed\n' "sync conflict preserves theirs" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
printf 'PASS — sync-workspace conflict fallback preserves the discarded side\n'
