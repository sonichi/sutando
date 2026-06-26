#!/usr/bin/env bash
# Tests for Bug #3 fix (Lucy's Maddy report 2026-06-06): pre-migration backup
# `tar -czf` froze 30+ min on a 34GB workspace with 32GB of incompressible mp4.
# Two-pronged fix:
#   1. Default-exclude media extensions + `notes/asset-library/` from the backup.
#   2. Skip gzip when surface payload > SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB.
# Both configurable; `SUTANDO_MIGRATE_NO_EXCLUDE=1` opts out.

set -u
# NOTE: deliberately not using `set -e` — these tests accumulate failures via
# `fail=1` and report at the end; a transient grep-no-match exiting 1 would
# kill the script before the assertion summary prints.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

fail=0

# Test 1: structural — default media excludes are declared
grep -q "_BACKUP_DEFAULT_EXCLUDES=" "$MIGRATE" \
    || { echo "  FAIL: _BACKUP_DEFAULT_EXCLUDES array missing"; fail=1; }
grep -qF '"*.mp4"' "$MIGRATE" \
    || { echo "  FAIL: *.mp4 not in default excludes"; fail=1; }
grep -qF '"*.mov"' "$MIGRATE" \
    || { echo "  FAIL: *.mov not in default excludes"; fail=1; }
grep -qF '"notes/asset-library"' "$MIGRATE" \
    || { echo "  FAIL: notes/asset-library not in default excludes"; fail=1; }
grep -qF '"node_modules"' "$MIGRATE" \
    || { echo "  FAIL: node_modules not in default excludes (Lucy + Chi + Mini 2026-06-06)"; fail=1; }
grep -qF '".git"' "$MIGRATE" \
    || { echo "  FAIL: .git not in default excludes (Mini 2026-06-06)"; fail=1; }

# Test 2: structural — threshold env var + size estimate logic
grep -qF 'SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB' "$MIGRATE" \
    || { echo "  FAIL: SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB not honored"; fail=1; }
grep -qF '_surface_kb' "$MIGRATE" \
    || { echo "  FAIL: _surface_kb size estimate missing"; fail=1; }

# Test 3: structural — opt-out env var
grep -qF 'SUTANDO_MIGRATE_NO_EXCLUDE' "$MIGRATE" \
    || { echo "  FAIL: SUTANDO_MIGRATE_NO_EXCLUDE opt-out missing"; fail=1; }

# Test 4: structural — user-extensible via SUTANDO_MIGRATE_BACKUP_EXCLUDE
grep -qF 'SUTANDO_MIGRATE_BACKUP_EXCLUDE' "$MIGRATE" \
    || { echo "  FAIL: SUTANDO_MIGRATE_BACKUP_EXCLUDE extra-pattern hook missing"; fail=1; }

# Test 5: structural — rollback handles both .tar and .tar.gz
grep -qF 'migration-backup-$ROLLBACK_ID.tar.gz' "$MIGRATE" \
    || { echo "  FAIL: rollback .tar.gz path probe missing"; fail=1; }
grep -qF 'migration-backup-$ROLLBACK_ID.tar' "$MIGRATE" \
    || { echo "  FAIL: rollback .tar path probe missing"; fail=1; }
grep -qF 'tar -xf "$backup_path"' "$MIGRATE" \
    || { echo "  FAIL: rollback auto-detect tar -xf missing (should not hardcode -z)"; fail=1; }

# Test 6: E2E — backup excludes mp4 files
# Setup: create a tmp DEST with notes/ containing both .md and .mp4 files,
# trigger backup_dest indirectly via a commit run, then list the tarball contents.
TMP_E2E="$(mktemp -d -t sutando-mig-backup-e2e.XXXXXX)"
mkdir -p "$TMP_E2E/src-c/notes" "$TMP_E2E/dest/notes/asset-library" "$TMP_E2E/dest/state"
echo "src-content" > "$TMP_E2E/src-c/notes/foo.md"
# Pre-populate dest with a notes/ directory mixing kept + excluded content
echo "kept-content" > "$TMP_E2E/dest/notes/should-be-in-backup.md"
echo "fake-mp4-content" > "$TMP_E2E/dest/notes/asset-library/bigvideo.mp4"
echo "another-fake-mp4" > "$TMP_E2E/dest/notes/some-clip.mp4"

# Trigger commit (and thus backup_dest). Bounded with kill-timer.
out="$(
    ( SUTANDO_MIGRATE_DEST="$TMP_E2E/dest" \
      bash "$MIGRATE" commit --source C --no-confirm --no-claude-import < /dev/null 2>&1 &
      PID=$!
      _slept=0
      while [ "$_slept" -lt 120 ]; do
          if ! kill -0 "$PID" 2>/dev/null; then break; fi
          sleep 1
          _slept=$((_slept+1))
      done
      kill -9 "$PID" 2>/dev/null || true
      wait "$PID" 2>/dev/null || true
    ) | tail -200
)"

# Find the backup tarball that was created
backup_file=""
if [ -d "$TMP_E2E/dest/state" ]; then
    backup_file="$(ls -1 "$TMP_E2E/dest/state/migration-backup-"*.tar.gz "$TMP_E2E/dest/state/migration-backup-"*.tar 2>/dev/null | head -1)"
fi

if [ -z "$backup_file" ] || [ ! -f "$backup_file" ]; then
    echo "  FAIL: E2E — backup tarball not created in $TMP_E2E/dest/state/"
    fail=1
else
    # List tarball contents — auto-detect via tar -tf (works for both gz and plain)
    backup_contents="$(tar -tf "$backup_file" 2>/dev/null)"
    # Should INCLUDE the .md file
    if ! echo "$backup_contents" | grep -q "notes/should-be-in-backup.md"; then
        echo "  FAIL: E2E — .md file missing from backup tarball (default excludes too aggressive?)"
        echo "    Backup contents:"
        echo "$backup_contents" | head -10 | sed 's/^/      /'
        fail=1
    fi
    # Should EXCLUDE the .mp4 file at notes/asset-library/
    if echo "$backup_contents" | grep -q "asset-library/bigvideo.mp4"; then
        echo "  FAIL: E2E — notes/asset-library/bigvideo.mp4 still in backup (notes/asset-library not excluded)"
        fail=1
    fi
    # Should EXCLUDE the .mp4 file directly under notes/
    if echo "$backup_contents" | grep -q "notes/some-clip.mp4"; then
        echo "  FAIL: E2E — notes/some-clip.mp4 still in backup (*.mp4 not excluded)"
        fail=1
    fi
fi

# Cleanup
rm -rf "$TMP_E2E"

# Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
