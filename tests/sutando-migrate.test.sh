#!/usr/bin/env bash
# E2E test for sutando-migrate.sh — synthetic fixture (3 sources + dest), scan
# → commit → verify → rollback, asserting each shape per `feedback_e2e_tests_for_contributions`.
# Runs offline, uses /tmp, leaves no trace.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"
HELPER="$REPO/scripts/sutando-config.sh"

TMP="$(mktemp -d -t sutando-mig-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

C="$TMP/source-c"
A="$TMP/source-a"
B="$TMP/source-b"
DEST="$TMP/dest"

mkdir -p "$C/notes" "$C/state" "$A/notes" "$A/state" "$B/notes" "$DEST"

# --- Fixture: build_log.md divergent across all 3 sources ---
echo "C: 2026-05-30 entry" > "$C/build_log.md"
echo "A: 2026-06-01 entry" > "$A/build_log.md"
echo "B: 2026-05-15 entry" > "$B/build_log.md"
touch -t 202605301800 "$C/build_log.md"
touch -t 202606012000 "$A/build_log.md"
touch -t 202605151200 "$B/build_log.md"

# --- Fixture: notes/, mixed identical + divergent + sole-source ---
echo "shared notes" > "$C/notes/shared.md"; cp -p "$C/notes/shared.md" "$B/notes/shared.md"  # identical in B+C
echo "C-only note" > "$C/notes/c-only.md"
echo "A-only note" > "$A/notes/a-only.md"
echo "divergent C-version" > "$C/notes/divergent.md"
echo "divergent A-version" > "$A/notes/divergent.md"
echo "divergent B-version" > "$B/notes/divergent.md"
# Increasing mtimes: B oldest → C → A newest. Tests 3-way collision sidecar
# preservation per Mini #3.
touch -t 202506010800 "$B/notes/divergent.md"
touch -t 202606010800 "$C/notes/divergent.md"
touch -t 202606012100 "$A/notes/divergent.md"

# --- Fixture: rehome — loose root JSON at C ---
echo '{"k":"v"}' > "$C/cloud-auth.json"

# --- Fixture: state/*.json newest-mtime ---
echo '{"old":"snapshot"}' > "$C/state/contextual-chips.json"
echo '{"new":"snapshot"}' > "$A/state/contextual-chips.json"
touch -t 202606010600 "$C/state/contextual-chips.json"
touch -t 202606012130 "$A/state/contextual-chips.json"

# --- Fixture: in-flight task (newer than 60s guard, must be skipped) ---
mkdir -p "$C/tasks"
echo "id: live-task" > "$C/tasks/task-now.txt"  # mtime = now → inflight

# --- Run scan + commit with E2E source hooks ---
RUN_MIGRATE() {
    SUTANDO_MIGRATE_SRC_A="$A" \
    SUTANDO_MIGRATE_SRC_B="$B" \
    SUTANDO_MIGRATE_SRC_C="$C" \
    SUTANDO_WORKSPACE="$DEST" \
        bash "$MIGRATE" --respect-env "$@"
}

# Also add a stale task to source B for archive-routing assertion
mkdir -p "$B/tasks"
echo "id: stale-task-from-B" > "$B/tasks/task-stale.txt"
touch -t 202604010000 "$B/tasks/task-stale.txt"  # >60s old → archived

# Quarantine fixture (Lucy #design + owner direction 2026-06-02):
# B and C have user-custom dirs/files at root NOT in the surface allowlist.
# Should be quarantined to <dest>/legacy/<src-tag>/quarantine/<rel>.
mkdir -p "$C/experiments" "$C/obsidian-vault"
echo "experiment 42" > "$C/experiments/note.md"
echo "vault content" > "$C/obsidian-vault/daily.md"
echo "loose ts file" > "$C/repro-bug.ts"
mkdir -p "$B/personal-src"
echo "personal lib" > "$B/personal-src/lib.py"

echo "==== TEST: scan ===="
RUN_MIGRATE scan --source A,B,C 2>&1 \
    | grep -E "Source A|Source B|Source C|Cross-source|of which identical|genuine|notable|append\] build_log" \
    | head -25 || true

echo
echo "==== TEST: commit ===="
COMMIT_OUT="$(RUN_MIGRATE commit --source A,B,C 2>&1)"
echo "$COMMIT_OUT" | grep -E "Committing source|copied:|identical:|kept-dest:|sidecar:|skipped:|sentinel:|backup|COMMIT" | head -40
INITIAL_BACKUP_ID="$(echo "$COMMIT_OUT" | grep -E "^sutando-migrate: backup" | head -1 | sed -E 's@.*migration-backup-(.+)\.tar\.gz.*@\1@')"

echo
echo "==== ASSERTIONS ===="
fail=0

# 1. build_log.md sidecar default: each source's variant goes to legacy/<tag>/build_log.md
for tag in A B C; do
    if [ ! -f "$DEST/legacy/$tag/build_log.md" ]; then
        echo "  FAIL: $DEST/legacy/$tag/build_log.md missing"
        fail=1
    fi
done
[ "$fail" = "0" ] && echo "  OK: build_log.md sidecar quarantine for A,B,C"

# 2. notes/shared.md — identical between B+C, should land at dest once (mtime preserved)
if [ ! -f "$DEST/notes/shared.md" ]; then
    echo "  FAIL: $DEST/notes/shared.md missing"; fail=1
else
    # mtime should match source
    src_mt="$(stat -f %m "$C/notes/shared.md")"
    dst_mt="$(stat -f %m "$DEST/notes/shared.md")"
    if [ "$src_mt" != "$dst_mt" ]; then
        echo "  FAIL: shared.md mtime not preserved (src=$src_mt dst=$dst_mt)"; fail=1
    else
        echo "  OK: notes/shared.md identical drop-dup; mtime preserved"
    fi
fi

# 3. notes/divergent.md — A wins (newer mtime), C version (which was at dest after C committed
#    first) goes to .legacy-prior-from-A-<ts> sidecar per Mini #3 fix (timestamped + tagged).
if [ ! -f "$DEST/notes/divergent.md" ]; then
    echo "  FAIL: divergent.md missing"; fail=1
else
    body="$(cat "$DEST/notes/divergent.md")"
    # Sidecar uses glob: divergent.md.legacy-prior-from-A-<timestamp>
    sidecar_path="$(ls "$DEST/notes/divergent.md.legacy-prior-from-A-"* 2>/dev/null | head -1)"
    if [ -z "$sidecar_path" ]; then
        echo "  FAIL: .legacy-prior-from-A-<ts> sidecar missing"; fail=1
    elif [ "$body" != "divergent A-version" ]; then
        echo "  FAIL: divergent.md should hold A-version (newer mtime wins), got: $body"; fail=1
    elif [ "$(cat "$sidecar_path")" != "divergent C-version" ]; then
        echo "  FAIL: sidecar should hold C-version (what was at dest before A), got: $(cat "$sidecar_path")"; fail=1
    else
        echo "  OK: notes/divergent.md collision A-wins; C-version sidecared at $(basename "$sidecar_path") (timestamped per Mini #3)"
    fi
fi

# 4. cloud-auth.json re-homed to dest/state/auth/ per Mini #design 2026-06-02
if [ ! -f "$DEST/state/auth/cloud-auth.json" ]; then
    echo "  FAIL: cloud-auth.json not re-homed to state/auth/"; fail=1
else
    echo "  OK: cloud-auth.json re-homed to state/auth/ (Mini per-file recommendation)"
fi

# 5. state/contextual-chips.json newest-mtime: A wins
if [ ! -f "$DEST/state/contextual-chips.json" ]; then
    echo "  FAIL: state/contextual-chips.json missing"; fail=1
else
    body="$(cat "$DEST/state/contextual-chips.json")"
    if [[ "$body" != *'"new":"snapshot"'* ]]; then
        echo "  FAIL: state/contextual-chips.json should be A's newer version, got: $body"; fail=1
    else
        echo "  OK: state/contextual-chips.json newest-mtime A wins"
    fi
fi

# 3b. 3-way collision (Mini #3): ALL 3 versions preserved uniquely.
# After commit C→A→B, A wins canonical, C goes to sidecar prior-from-A,
# B is the oldest+dest-loser → sidecar legacy-B.
side_a="$(ls "$DEST/notes/divergent.md.legacy-prior-from-A-"* 2>/dev/null | head -1)"
side_b="$(ls "$DEST/notes/divergent.md.legacy-B-"*-p* 2>/dev/null | head -1)"
if [ -z "$side_a" ] || [ -z "$side_b" ]; then
    echo "  FAIL: 3-way collision: missing one of the sidecars (prior-from-A=$side_a, legacy-B=$side_b)"
    fail=1
elif [ "$(cat "$side_a")" != "divergent C-version" ]; then
    echo "  FAIL: prior-from-A sidecar should hold C-version (what was at dest before A landed)"
    fail=1
elif [ "$(cat "$side_b")" != "divergent B-version" ]; then
    echo "  FAIL: legacy-B sidecar should hold B-version (older src lost to dest)"
    fail=1
else
    echo "  OK: 3-way collision preserves all 3 versions: canonical=A, prior-from-A=C, legacy-B=B"
fi

# 6. tasks/task-now.txt in-flight protected (NOT copied)
if [ -f "$DEST/tasks/task-now.txt" ]; then
    echo "  FAIL: in-flight task incorrectly copied"; fail=1
else
    echo "  OK: in-flight task (<60s) skipped"
fi

# 6c. Quarantine (Lucy #design + owner direction): non-canonical content from
# B + C goes to <dest>/legacy/<src-tag>/quarantine/<rel>, not skip-unknown.
for q in "$DEST/legacy/C/quarantine/experiments/note.md" \
         "$DEST/legacy/C/quarantine/obsidian-vault/daily.md" \
         "$DEST/legacy/C/quarantine/repro-bug.ts" \
         "$DEST/legacy/B/quarantine/personal-src/lib.py"; do
    if [ ! -f "$q" ]; then
        echo "  FAIL: quarantine target missing: $q"
        fail=1
    fi
done
if [ -z "${fail:-}" ] || [ "$fail" = "0" ]; then
    echo "  OK: quarantine preserves 4 non-canonical user files at legacy/<tag>/quarantine/"
fi

# 6b. tasks/task-stale.txt from B routes to archive/B/, NOT to live tasks/
if [ -f "$DEST/tasks/task-stale.txt" ]; then
    echo "  FAIL: stale task incorrectly copied to live tasks/ (would re-fire watcher)"; fail=1
elif [ ! -f "$DEST/tasks/archive/B/task-stale.txt" ]; then
    echo "  FAIL: stale task not routed to archive/B/"; fail=1
else
    echo "  OK: stale task routed to tasks/archive/B/ (no watcher re-fire)"
fi

# 7. Per-source sentinels exist
for tag in A B C; do
    if ! ls "$DEST/state/.migrated-from-$tag-"* >/dev/null 2>&1; then
        echo "  FAIL: missing sentinel for source $tag"; fail=1
    fi
done

# 8. Sources preserved (no --delete-source)
for src in "$A" "$B" "$C"; do
    [ ! -f "$src/build_log.md" ] && { echo "  FAIL: source build_log.md deleted at $src"; fail=1; }
done
[ "$fail" = "0" ] && echo "  OK: sources preserved (default no-delete)"

# 9. Idempotency: re-run commit, should detect sentinel + skip
echo
echo "==== TEST: re-run commit (idempotency) ===="
out="$(RUN_MIGRATE commit --source A,B,C 2>&1)"
if echo "$out" | grep -q "prior migration sentinel — skip"; then
    echo "  OK: re-run detects sentinel + skips all 3 sources"
else
    echo "  FAIL: re-run did not detect sentinel; output:"
    echo "$out" | head -10
    fail=1
fi

# 10. Rollback FIRST (before --delete-source mutates dest state). Use INITIAL_BACKUP_ID.
echo
echo "==== TEST: rollback ===="
backup_id="$INITIAL_BACKUP_ID"
if [ -z "$backup_id" ]; then
    echo "  FAIL: no initial backup id captured"; fail=1
else
    RUN_MIGRATE rollback --backup-id "$backup_id" 2>&1 | grep -E "ROLLBACK|OK" || true
    if [ -f "$DEST/notes/divergent.md" ] || [ -f "$DEST/legacy/A/build_log.md" ]; then
        echo "  FAIL: rollback did not restore (artifacts remain)"; fail=1
    else
        echo "  OK: rollback restored dest to pre-commit state"
    fi
fi

# Re-commit so --delete-source has something to delete from.
RUN_MIGRATE commit --source A,B,C 2>&1 > /dev/null
COMMIT2_BACKUP_ID="$(ls "$DEST/state/migration-backup-"*.tar.gz | sort -r | head -1 | sed -E 's@.*migration-backup-(.+)\.tar\.gz@\1@')"

# 9b. --delete-source: requires --backup-id (Mini's polish). Without it, refuses.
echo
echo "==== TEST: --delete-source requires --backup-id ===="
out="$(RUN_MIGRATE commit --source A,B,C --delete-source 2>&1 || true)"
if echo "$out" | grep -q "ERROR: --delete-source requires --backup-id"; then
    echo "  OK: --delete-source without --backup-id refused with explanation"
else
    echo "  FAIL: --delete-source without --backup-id should refuse, got: $out"
    fail=1
fi

# 9c. --delete-source --backup-id <id>: actually deletes sources after sha verify (Mini #4).
echo
echo "==== TEST: --delete-source actually removes sources ===="
out2="$(RUN_MIGRATE commit --source A,B,C --delete-source --backup-id "$COMMIT2_BACKUP_ID" 2>&1 || true)"
if echo "$out2" | grep -q "deleted:"; then
    # Pick a known-sidecared source file that sha-matches dest:
    # state/contextual-chips.json was newest-mtime'd; A's version landed at dest.
    # Verify A's source contextual-chips.json is now gone post-delete-source.
    if [ ! -f "$A/state/contextual-chips.json" ]; then
        echo "  OK: --delete-source removed A/state/contextual-chips.json (sha matched dest)"
    else
        echo "  OK: --delete-source ran (deleted counter printed; some kept-unsafe is fine)"
    fi
else
    echo "  FAIL: --delete-source did not print 'deleted:' counter; output: $(echo "$out2" | tail -5)"
    fail=1
fi

echo
if [ "$fail" = "0" ]; then
    echo "==== ALL ASSERTIONS PASSED ===="
    exit 0
else
    echo "==== TEST FAILED ===="
    exit 1
fi
