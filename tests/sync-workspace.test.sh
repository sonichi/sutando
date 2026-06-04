#!/usr/bin/env bash
# Integration tests for scripts/sync-workspace.sh — PR-1 (Phase 4, post 05:13Z simplification).
#
# Post-simplification design (owner directive 05:11Z + 05:15Z): no canonical_id
# translation layer; each host's Claude Code memory dir is tracked under its
# OWN slug at `.claude-sutando/projects/<local_slug>/memory/`. After pull,
# peer slug subdirs are visible-not-merged. Only the `memory/` subdir within
# each slug is tracked — transcripts + file_history stay gitignored.
#
# Hermetic: never touches the operator's real workspace or vault.
#
# Run: bash tests/sync-workspace.test.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

TEST_ROOT="$(mktemp -d -t sync-workspace-test.XXXXXX)"
trap "rm -rf '$TEST_ROOT'" EXIT

fail=0
pass=0
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — expected '$expected', got '$actual'"; fail=$((fail+1))
  fi
}
assert_file_exists() {
  local desc="$1" path="$2"
  if [ -f "$path" ]; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — '$path' missing"; fail=$((fail+1))
  fi
}
assert_dir_exists() {
  local desc="$1" path="$2"
  if [ -d "$path" ]; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — '$path' missing"; fail=$((fail+1))
  fi
}
assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if grep -qF "$needle" <<<"$haystack" 2>/dev/null || [ -f "$haystack" -a -n "$(grep -F "$needle" "$haystack" 2>/dev/null)" ]; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — '$needle' not in haystack"; fail=$((fail+1))
  fi
}
assert_not_in_vault() {
  local desc="$1" branch="$2" path="$3"
  if git --git-dir="$FIXTURE_VAULT" show "${branch}:${path}" >/dev/null 2>&1; then
    echo "  FAIL: $desc — '$path' SHOULD NOT be in vault $branch but IS"; fail=$((fail+1))
  else
    echo "  OK: $desc"; pass=$((pass+1))
  fi
}
assert_in_vault() {
  local desc="$1" branch="$2" path="$3"
  if git --git-dir="$FIXTURE_VAULT" show "${branch}:${path}" >/dev/null 2>&1; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — '$path' NOT in vault $branch"; fail=$((fail+1))
  fi
}

# ---- Fixture setup ----
FIXTURE_REPO="$TEST_ROOT/sutando"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src" "$FIXTURE_REPO/skills"
touch "$FIXTURE_REPO/CLAUDE.md"
git init -q "$FIXTURE_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cat > "$FIXTURE_REPO/sutando.config.json" <<JSON
{"workspace": {"path": "\${REPO_DIR}/workspace"}, "vault": {"enabled": false}}
JSON

FIXTURE_WS_RAW="$TEST_ROOT/workspace"
mkdir -p "$FIXTURE_WS_RAW"
if command -v realpath >/dev/null 2>&1; then
  FIXTURE_WS="$(realpath "$FIXTURE_WS_RAW")"
else
  FIXTURE_WS="$FIXTURE_WS_RAW"
fi

FIXTURE_VAULT="$TEST_ROOT/vault.git"
git init -q --bare "$FIXTURE_VAULT"

COMMON_ENV=(
  SUTANDO_REPO_DIR="$FIXTURE_REPO"
  SUTANDO_WORKSPACE="$FIXTURE_WS"
  SUTANDO_TEST_MODE=1
  SUTANDO_VAULT="$FIXTURE_VAULT"
)

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
HOST=$(hostname | sed 's/\..*//')
HOST_BRANCH="refs/heads/host/${HOST}"

# local_slug = REPO_DIR with / replaced by - (mirror script + Claude Code)
LOCAL_SLUG=$(printf '%s' "$FIXTURE_REPO" | sed 's|/|-|g')
LOCAL_MEM_DIR="$FIXTURE_WS/.claude-sutando/projects/${LOCAL_SLUG}/memory"

run_sync() {
  env "${COMMON_ENV[@]}" bash "$SYNC" "$@"
}

# ============================================================================
echo "==== Test 1: --status before init ===="
out_status=$(run_sync --status 2>&1)
case "$out_status" in
  *"WORKSPACE_DIR: $FIXTURE_WS"*)
    echo "  OK: --status shows correct WORKSPACE_DIR"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --status WORKSPACE_DIR missing or wrong: $out_status"; fail=$((fail+1)) ;;
esac
case "$out_status" in
  *"VAULT_URL:     $FIXTURE_VAULT"*)
    echo "  OK: --status shows correct VAULT_URL"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --status VAULT_URL missing"; fail=$((fail+1)) ;;
esac
case "$out_status" in
  *"NOT a git repo"*)
    echo "  OK: --status reports not-yet-a-git-repo before init"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --status didn't note workspace isn't a git repo: $out_status"; fail=$((fail+1)) ;;
esac
# Verify the OLD translation-layer fields are GONE from --status
case "$out_status" in
  *"canonical_id"*|*"projects.map.json"*)
    echo "  FAIL: --status still mentions removed translation-layer fields: $out_status"; fail=$((fail+1)) ;;
  *) echo "  OK: --status no longer mentions canonical_id / projects.map.json"; pass=$((pass+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 2: --init creates .gitignore + .git + first push ===="
run_sync --init 2>&1 | head -10

assert_dir_exists ".git exists in workspace"  "$FIXTURE_WS/.git"
assert_file_exists ".gitignore created"       "$FIXTURE_WS/.gitignore"

# .gitignore content sanity
assert_contains ".gitignore whitelists notes/"           "!notes/"                                 "$FIXTURE_WS/.gitignore"
assert_contains ".gitignore tracks memory subdirs"        "!.claude-sutando/projects/*/memory/"     "$FIXTURE_WS/.gitignore"
assert_contains ".gitignore tracks memory contents"       "!.claude-sutando/projects/*/memory/**"   "$FIXTURE_WS/.gitignore"
assert_contains ".gitignore hard-denies .env"             ".env*"                                   "$FIXTURE_WS/.gitignore"

# Verify the OLD canonical-specific pattern is GONE
if grep -qE 'projects/[a-f0-9]{8}/memory' "$FIXTURE_WS/.gitignore"; then
  echo "  FAIL: .gitignore still has canonical-id-specific pattern"; fail=$((fail+1))
else
  echo "  OK: .gitignore no longer has canonical-id-specific pattern"; pass=$((pass+1))
fi

# .sutando-vault/projects.map.json should NOT be created
if [ -f "$FIXTURE_WS/.sutando-vault/projects.map.json" ]; then
  echo "  FAIL: .sutando-vault/projects.map.json should NOT be created (translation layer removed)"; fail=$((fail+1))
else
  echo "  OK: .sutando-vault/projects.map.json correctly NOT created"; pass=$((pass+1))
fi

# Vault remote configured
REMOTE_URL=$(cd "$FIXTURE_WS" && git remote get-url origin)
assert_eq "git remote origin = vault"  "$FIXTURE_VAULT"  "$REMOTE_URL"

# host/<hostname> branch exists in bare repo
if git --git-dir="$FIXTURE_VAULT" rev-parse "$HOST_BRANCH" >/dev/null 2>&1; then
  echo "  OK: host/${HOST} branch pushed to vault"; pass=$((pass+1))
else
  echo "  FAIL: host/${HOST} branch NOT in vault"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 3: idempotent re-init ===="
out_reinit=$(run_sync --init 2>&1)
if echo "$out_reinit" | grep -q "already a git repo"; then
  echo "  OK: re-init detects existing repo"; pass=$((pass+1))
else
  echo "  INFO: re-init output: $out_reinit"
fi

# ============================================================================
echo
echo "==== Test 4: --push-only with no changes is a no-op ===="
out_noop=$(run_sync --push-only 2>&1)
case "$out_noop" in
  *"nothing to push"*) echo "  OK: --push-only no-op when clean"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --push-only didn't detect clean tree: $out_noop"; fail=$((fail+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 5: write memory to local-slug, push, verify in vault under SAME slug (no translation) ===="
mkdir -p "$LOCAL_MEM_DIR"
echo "test memory content from $HOST" > "$LOCAL_MEM_DIR/feedback_test.md"

run_sync --push-only 2>&1 | head -5

# Memory file should appear in vault under the local_slug path (NOT under any canonical)
assert_in_vault "feedback_test.md present in vault under local_slug path" \
                "$HOST_BRANCH" \
                ".claude-sutando/projects/${LOCAL_SLUG}/memory/feedback_test.md"

# ============================================================================
echo
echo "==== Test 6: write a fake transcript to local-slug — must NOT be pushed (only memory/ tracked) ===="
mkdir -p "$FIXTURE_WS/.claude-sutando/projects/${LOCAL_SLUG}/transcripts"
echo "this is a fake transcript line" > "$FIXTURE_WS/.claude-sutando/projects/${LOCAL_SLUG}/transcripts/session-1.jsonl"

# Run sync — transcript should be ignored by .gitignore
run_sync --push-only 2>&1 | head -3

assert_not_in_vault "transcript file NOT pushed (gitignored — only memory/ tracked)" \
                    "$HOST_BRANCH" \
                    ".claude-sutando/projects/${LOCAL_SLUG}/transcripts/session-1.jsonl"

# ============================================================================
echo
echo "==== Test 7: peer host pushes to its own slug, pull, verify peer slug visible ===="
PEER_WS="$TEST_ROOT/peer-workspace"
git clone -q "$FIXTURE_VAULT" "$PEER_WS" 2>/dev/null
PEER_SLUG="-Users-peer-sutando"
(
    cd "$PEER_WS"
    git checkout -B "host/peerhost" "origin/host/${HOST}" >/dev/null 2>&1
    mkdir -p ".claude-sutando/projects/${PEER_SLUG}/memory"
    echo "from peer" > ".claude-sutando/projects/${PEER_SLUG}/memory/feedback_peer.md"
    git add -A
    git -c user.email=peer@test -c user.name=peer commit -q -m "peer write" >/dev/null 2>&1
    git push -q origin "host/peerhost" 2>/dev/null
)

# Pull on host 1
run_sync --pull-only 2>&1 | head -5

# Verify peer's slug subdir is visible in host 1's workspace
PEER_MEM_FILE="$FIXTURE_WS/.claude-sutando/projects/${PEER_SLUG}/memory/feedback_peer.md"
assert_file_exists "peer's memory visible in host 1's workspace under peer slug" "$PEER_MEM_FILE"

# Verify host 1's OWN memory still present (peer pull didn't clobber it)
assert_file_exists "host 1's own memory still present after pull" \
                   "$LOCAL_MEM_DIR/feedback_test.md"

# ============================================================================
echo
echo "==== Test 8: mass-deletion tripwire ===="
cd "$FIXTURE_WS"
mkdir -p notes
for i in $(seq 1 60); do echo "n$i" > "notes/note-$i.md"; done
git add notes/
git -c user.email=test@test -c user.name=test commit -q -m "60 notes"
rm -f notes/note-*.md
cd - >/dev/null

out_tripwire=$(run_sync --push-only 2>&1 || true)
case "$out_tripwire" in
  *"refusing push"*|*"tripwire"*|*"would delete"*)
    echo "  OK: mass-deletion tripwire fired"; pass=$((pass+1)) ;;
  *) echo "  FAIL: tripwire didn't fire on 60 deletions: $out_tripwire"; fail=$((fail+1)) ;;
esac

cd "$FIXTURE_WS"
if [ -z "$(git diff --cached --name-only)" ]; then
  echo "  OK: tripwire reset staged changes"; pass=$((pass+1))
else
  echo "  FAIL: tripwire didn't reset staged changes"; fail=$((fail+1))
fi
cd - >/dev/null

# ============================================================================
echo
echo "==== Test 9: .gitignore overwrite warning (Pro #1445 review fix #3) ===="
# Modify the .gitignore in place, then run --init without --force-gitignore.
# Expected: refuse + print diff.
echo "# my custom user edit" >> "$FIXTURE_WS/.gitignore"
out_overwrite=$(run_sync --init 2>&1 || true)
case "$out_overwrite" in
  *"Refusing to overwrite"*)
    echo "  OK: --init refuses to overwrite user-edited .gitignore"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --init silently overwrote user-edited .gitignore: $out_overwrite"; fail=$((fail+1)) ;;
esac
# Verify the user's edit survived
if grep -q "my custom user edit" "$FIXTURE_WS/.gitignore"; then
  echo "  OK: user's custom .gitignore line preserved (not overwritten)"; pass=$((pass+1))
else
  echo "  FAIL: user's custom .gitignore line was lost"; fail=$((fail+1))
fi

# Now with --force-gitignore → should overwrite
out_force=$(env "${COMMON_ENV[@]}" bash "$SYNC" --init --force-gitignore 2>&1 || true)
if grep -q "my custom user edit" "$FIXTURE_WS/.gitignore"; then
  echo "  FAIL: --force-gitignore didn't overwrite (user edit still there)"; fail=$((fail+1))
else
  echo "  OK: --force-gitignore did overwrite"; pass=$((pass+1))
fi

# ============================================================================
echo
echo "==== Test 10: pull-side mass-deletion tripwire (Pro #1445 review fix #2) ===="
# Setup: host 1 has many notes pushed. Peer creates a branch, deletes them all,
# pushes. Host 1 --pull-only should detect the mass-delete via merge + reset.

# First, make sure host 1 has the 60 notes pushed (they were committed in Test 8
# but the tripwire reset prevented push; let's force-push them now via SUTANDO_FORCE_SYNC).
cd "$FIXTURE_WS"
for i in $(seq 1 60); do echo "n$i" > "notes/note-$i.md"; done
SUTANDO_FORCE_SYNC=1 env "${COMMON_ENV[@]}" bash "$SYNC" --push-only 2>&1 | head -3
cd - >/dev/null

# Peer: clone, delete all 60 notes, push to host/peerhost2
PEER2_WS="$TEST_ROOT/peer2-workspace"
git clone -q "$FIXTURE_VAULT" "$PEER2_WS" 2>/dev/null
(
    cd "$PEER2_WS"
    git checkout -B "host/peerhost2" "origin/host/${HOST}" >/dev/null 2>&1
    rm -f notes/note-*.md
    git add -A
    git -c user.email=peer2@test -c user.name=peer2 commit -q -m "peer2 deletes all notes" >/dev/null 2>&1
    git push -q origin "host/peerhost2" 2>/dev/null
)

# Host 1: pull → should detect mass-delete + reset
PRE_NOTE_COUNT=$(ls "$FIXTURE_WS/notes/note-"*.md 2>/dev/null | wc -l | tr -d ' ')
out_pull_trip=$(run_sync --pull-only 2>&1 || true)
case "$out_pull_trip" in
  *"REFUSING pull"*|*"tripwire"*|*"deleted"*)
    echo "  OK: pull-side tripwire fired on peer mass-deletion"; pass=$((pass+1)) ;;
  *) echo "  FAIL: pull-side tripwire did NOT fire on peer's 60-file deletion: $out_pull_trip"; fail=$((fail+1)) ;;
esac

POST_NOTE_COUNT=$(ls "$FIXTURE_WS/notes/note-"*.md 2>/dev/null | wc -l | tr -d ' ')
if [ "$POST_NOTE_COUNT" -ge "$PRE_NOTE_COUNT" ]; then
  echo "  OK: working tree restored after tripwire (had $PRE_NOTE_COUNT, now $POST_NOTE_COUNT)"; pass=$((pass+1))
else
  echo "  FAIL: tripwire didn't restore working tree ($PRE_NOTE_COUNT → $POST_NOTE_COUNT)"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 11: --dry-run for --migrate-from-legacy (Pro #1445 review fix #1) ===="
# Setup a fake legacy ~/.sutando/memory-sync/-style clone with notes + memory
LEGACY_FIXTURE="$TEST_ROOT/fake-legacy-memory-sync"
git init -q "$LEGACY_FIXTURE"
mkdir -p "$LEGACY_FIXTURE/notes" "$LEGACY_FIXTURE/memory"
echo "legacy note" > "$LEGACY_FIXTURE/notes/legacy-note.md"
echo "legacy memory" > "$LEGACY_FIXTURE/memory/feedback_legacy.md"
echo "legacy pending" > "$LEGACY_FIXTURE/pending-questions.md"
echo "legacy build" > "$LEGACY_FIXTURE/build_log.md"
(cd "$LEGACY_FIXTURE" && git add -A && git -c user.email=test@test -c user.name=test commit -q -m "legacy fixture")

# Snapshot workspace state pre-migrate
PRE_NOTES_COUNT=$(find "$FIXTURE_WS/notes" -type f 2>/dev/null | wc -l | tr -d ' ')
PRE_LEGACY_NOTE_EXISTS="$([ -f "$FIXTURE_WS/notes/legacy-note.md" ] && echo yes || echo no)"

# Run --dry-run; should NOT mutate fs
out_dryrun=$(SUTANDO_MEMORY_SYNC_DIR="$LEGACY_FIXTURE" \
             env "${COMMON_ENV[@]}" bash "$SYNC" --migrate-from-legacy --dry-run 2>&1 || true)
case "$out_dryrun" in
  *"DRY-RUN"*)
    echo "  OK: --dry-run output contains DRY-RUN markers"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --dry-run didn't produce DRY-RUN markers: $out_dryrun"; fail=$((fail+1)) ;;
esac

# Verify legacy note was NOT copied to workspace
if [ -f "$FIXTURE_WS/notes/legacy-note.md" ] && [ "$PRE_LEGACY_NOTE_EXISTS" = "no" ]; then
  echo "  FAIL: --dry-run copied legacy-note.md anyway"; fail=$((fail+1))
else
  echo "  OK: --dry-run did NOT copy legacy-note.md"; pass=$((pass+1))
fi

# Note count unchanged
POST_NOTES_COUNT=$(find "$FIXTURE_WS/notes" -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$POST_NOTES_COUNT" = "$PRE_NOTES_COUNT" ]; then
  echo "  OK: --dry-run kept workspace notes count unchanged ($PRE_NOTES_COUNT → $POST_NOTES_COUNT)"; pass=$((pass+1))
else
  echo "  FAIL: --dry-run changed notes count ($PRE_NOTES_COUNT → $POST_NOTES_COUNT)"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 12: pull-side delete-AND-add bypass (Mini #1445 v3 Medium fix) ===="
# Setup: host 1 has 60 notes pushed (from Test 10 cleanup). Peer's branch deletes
# all 60 + adds 60 new files → net tracked-file count is unchanged, but actual
# deletions = 60. Pre-fix tripwire (which used pre_count - post_count = 0) would
# bypass; post-fix tripwire counts actual diff-D deletions and fires.

# Reset host 1 to have the 60 notes pushed
cd "$FIXTURE_WS"
for i in $(seq 1 60); do echo "n$i" > "notes/note-$i.md"; done
SUTANDO_FORCE_SYNC=1 env "${COMMON_ENV[@]}" bash "$SYNC" --push-only 2>&1 | head -3
cd - >/dev/null

# Peer3: clone, delete all 60 notes, add 60 NEW files (net zero), push
PEER3_WS="$TEST_ROOT/peer3-workspace"
git clone -q "$FIXTURE_VAULT" "$PEER3_WS" 2>/dev/null
(
    cd "$PEER3_WS"
    git checkout -B "host/peerhost3" "origin/host/${HOST}" >/dev/null 2>&1
    rm -f notes/note-*.md
    for i in $(seq 1 60); do echo "n$i" > "notes/replacement-$i.md"; done
    git add -A
    git -c user.email=peer3@test -c user.name=peer3 commit -q -m "peer3 deletes 60 + adds 60 (net zero)" >/dev/null 2>&1
    git push -q origin "host/peerhost3" 2>/dev/null
)

# Host 1: pull — should detect actual 60-file deletion + reset (NOT bypass on net=0)
out_bypass=$(run_sync --pull-only 2>&1 || true)
case "$out_bypass" in
  *"REFUSING pull"*|*"deleted 60"*|*"tripwire"*)
    echo "  OK: actual-deletion-count tripwire fired on delete-and-add bypass attempt"; pass=$((pass+1)) ;;
  *) echo "  FAIL: delete-and-add bypass succeeded — tripwire missed: $out_bypass"; fail=$((fail+1)) ;;
esac

# Snapshot HEAD before pull was attempted (from earlier in test setup)
PRE_BYPASS_SHA=$(cd "$FIXTURE_WS" && git rev-parse HEAD 2>/dev/null)

# Verify host 1's note-*.md files survived
RESTORED_COUNT=$(ls "$FIXTURE_WS/notes/note-"*.md 2>/dev/null | wc -l | tr -d ' ')
if [ "$RESTORED_COUNT" -ge 60 ]; then
  echo "  OK: original notes restored after bypass-attempt tripwire ($RESTORED_COUNT files)"; pass=$((pass+1))
else
  echo "  FAIL: notes were lost ($RESTORED_COUNT remain, expected ≥60)"; fail=$((fail+1))
fi

# Mini #1445 v4 test gap: assert peer's replacement-*.md files NOT present
# NB: `find` (not `ls`) — ls of non-matching glob exits 2 + pipefail trips set-e
REPLACEMENT_COUNT=$(find "$FIXTURE_WS/notes" -maxdepth 1 -name 'replacement-*.md' 2>/dev/null | wc -l | tr -d ' ')
if [ "$REPLACEMENT_COUNT" = "0" ]; then
  echo "  OK: peer's replacement-*.md NOT pulled in (rolled back)"; pass=$((pass+1))
else
  echo "  FAIL: $REPLACEMENT_COUNT replacement-*.md files leaked into workspace"; fail=$((fail+1))
fi

# Mini #1445 v4 test gap: assert HEAD restored to pre-pull SHA
# (run --pull-only again to confirm tripwire still fires + leaves HEAD clean)
PRE_HEAD2=$(cd "$FIXTURE_WS" && git rev-parse HEAD 2>/dev/null)
run_sync --pull-only 2>&1 >/dev/null || true
POST_HEAD2=$(cd "$FIXTURE_WS" && git rev-parse HEAD 2>/dev/null)
if [ "$PRE_HEAD2" = "$POST_HEAD2" ]; then
  echo "  OK: HEAD unchanged across repeated tripwire pulls ($PRE_HEAD2)"; pass=$((pass+1))
else
  echo "  FAIL: HEAD drifted across pulls ($PRE_HEAD2 → $POST_HEAD2)"; fail=$((fail+1))
fi

# Mini #1445 v4 test gap: assert git status is clean (no leftover staged/unmerged)
WS_STATUS=$(cd "$FIXTURE_WS" && git status --porcelain 2>/dev/null)
if [ -z "$WS_STATUS" ]; then
  echo "  OK: git status clean after tripwire (no leftover staged/unmerged paths)"; pass=$((pass+1))
else
  echo "  FAIL: git status not clean after tripwire:"; printf '%s\n' "$WS_STATUS" | head -5; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 13: .env without SUTANDO_VAULT falls through to legacy (Mini #1445 v4 Medium) ===="
# Setup: write .env with ONLY SUTANDO_MEMORY_REPO (legacy alias), no
# SUTANDO_VAULT entry. Run --status WITHOUT SUTANDO_VAULT in env. Pre-fix,
# the `grep '^SUTANDO_VAULT=' | head -1 | ...` pipeline returned nonzero
# under set -euo pipefail and the script exited before the legacy-alias
# fallback could run.

# Write .env with only the legacy alias
echo "SUTANDO_MEMORY_REPO=$FIXTURE_VAULT" > "$FIXTURE_REPO/.env"

# Run --status WITHOUT SUTANDO_VAULT in env
out_legacy_alias=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --status 2>&1; echo "EXIT=$?")
legacy_exit=$(printf '%s' "$out_legacy_alias" | sed -n 's/^EXIT=//p' | tail -1)

if [ "$legacy_exit" = "0" ]; then
  echo "  OK: --status exits 0 when .env has only SUTANDO_MEMORY_REPO (no SUTANDO_VAULT)"; pass=$((pass+1))
else
  echo "  FAIL: --status exited $legacy_exit on legacy-alias .env (set-e tripped on grep): $out_legacy_alias"; fail=$((fail+1))
fi

case "$out_legacy_alias" in
  *"SUTANDO_MEMORY_REPO"*)
    echo "  OK: legacy-alias deprecation warning surfaced"; pass=$((pass+1)) ;;
  *) echo "  FAIL: legacy-alias deprecation warning missing: $out_legacy_alias"; fail=$((fail+1)) ;;
esac

# Cleanup so subsequent test runs aren't sticky
rm -f "$FIXTURE_REPO/.env"

# ============================================================================
echo
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
