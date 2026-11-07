#!/usr/bin/env bash
# Rollback cleanup keeps filename boundaries and stays inside the dest.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

TMP="$(mktemp -d -t sutando-rb-names.XXXXXX)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
fail=0
NL=$'\n'

ROLLBACK() {
    SUTANDO_MIGRATE_DEST="$1" bash "$MIGRATE" rollback --backup-id "$2" 2>&1
}

# 1. A post-backup file whose name embeds "\n<absolute path>" must not reach that path.
D="$TMP/d1"; OUT="$TMP/outside"
mkdir -p "$D/notes" "$D/state" "$OUT"
echo keep > "$OUT/victim.txt"
echo old > "$D/notes/old.md"
echo bracket > "$D/notes/[draft] a*.md"
mkdir -p "$D/notes/kept${NL}dir"; echo nl > "$D/notes/kept${NL}dir/in-backup.md"
( cd "$D" && tar -czf state/migration-backup-T1.tar.gz notes )
mkdir -p "$D/notes/x${NL}$OUT"
echo injected > "$D/notes/x${NL}$OUT/victim.txt"
echo added > "$D/notes/added.md"
out="$(ROLLBACK "$D" T1)"; rc=$?
[ "$rc" = 0 ] || { echo "  FAIL: rollback exited $rc: $out"; fail=1; }
[ -f "$OUT/victim.txt" ] && echo "  OK: file outside the dest survives rollback" \
    || { echo "  FAIL: rollback deleted a file outside the dest"; fail=1; }
[ ! -e "$D/notes/x${NL}$OUT/victim.txt" ] && echo "  OK: newline-named post-backup file removed as one name" \
    || { echo "  FAIL: newline-named post-backup file was left behind"; fail=1; }
[ ! -e "$D/notes/added.md" ] && echo "  OK: post-backup file removed" \
    || { echo "  FAIL: post-backup notes/added.md survived rollback"; fail=1; }
for f in "notes/old.md" "notes/[draft] a*.md" "notes/kept${NL}dir/in-backup.md"; do
    [ -f "$D/$f" ] && echo "  OK: backed-up $(printf %q "$f") kept" \
        || { echo "  FAIL: backed-up $(printf %q "$f") was deleted"; fail=1; }
done

# 2. A plain (uncompressed) .tar backup is listed too.
D="$TMP/d2"; mkdir -p "$D/notes" "$D/state"
echo old > "$D/notes/old.md"
( cd "$D" && tar -cf state/migration-backup-T2.tar notes )
echo added > "$D/notes/added.md"
ROLLBACK "$D" T2 >/dev/null; rc=$?
{ [ "$rc" = 0 ] && [ -f "$D/notes/old.md" ] && [ ! -e "$D/notes/added.md" ]; } \
    && echo "  OK: plain .tar backup restores and cleans" \
    || { echo "  FAIL: plain .tar rollback rc=$rc"; fail=1; }

# 3. An unreadable non-empty backup refuses cleanup instead of mass-deleting.
D="$TMP/d3"; mkdir -p "$D/notes" "$D/state"
echo precious > "$D/notes/precious.md"
echo "not a tar" > "$D/state/migration-backup-T3.tar"
out="$(ROLLBACK "$D" T3)"; rc=$?
{ [ "$rc" != 0 ] && [ -f "$D/notes/precious.md" ]; } \
    && echo "  OK: unreadable backup refuses cleanup and deletes nothing" \
    || { echo "  FAIL: unreadable backup rc=$rc precious=$( [ -f "$D/notes/precious.md" ] && echo kept || echo deleted)"; fail=1; }

# 4. A symlinked surface dir pointing outside the dest is not walked.
D="$TMP/d4"; EXT="$TMP/ext"; mkdir -p "$D/state" "$EXT"
echo ext > "$EXT/ext.md"
: > "$D/state/migration-backup-T4.tar.gz"
ln -s "$EXT" "$D/notes"
ROLLBACK "$D" T4 >/dev/null
[ -f "$EXT/ext.md" ] && echo "  OK: symlinked surface dir is not followed" \
    || { echo "  FAIL: rollback followed a symlinked surface dir out of the dest"; fail=1; }

if [ "$fail" = 0 ]; then echo "==== ALL ASSERTIONS PASSED ===="; else echo "==== TEST FAILED ===="; fi
exit "$fail"
