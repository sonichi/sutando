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
cat > "$FIXTURE_REPO/sutando.config.json" <<'JSON'
{
  "workspace": {"path": "${REPO_DIR}/workspace"},
  "vault": {
    "enabled": false,
    "sync": {
      "include": [
        "notes/",
        "pending-questions.md",
        "build_log.md",
        ".claude-sutando/projects/*/memory/"
      ],
      "exclude": []
    }
  }
}
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
)

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
HOST=$(hostname | sed 's/\..*//')
HOST_BRANCH="refs/heads/host/${HOST}"

# local_slug = REPO_DIR with / replaced by - (mirror script + Claude Code)
LOCAL_SLUG=$(printf '%s' "$FIXTURE_REPO" | sed 's|/|-|g')
LOCAL_MEM_DIR="$FIXTURE_WS/.claude-sutando/projects/${LOCAL_SLUG}/memory"

# PR-2: SUTANDO_VAULT env var removed; tests pass vault via --vault-url flag.
run_sync() {
  env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" "$@"
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
out_force=$(env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --init --force-gitignore 2>&1 || true)
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
SUTANDO_FORCE_SYNC=1 env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --push-only 2>&1 | head -3
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
# `find` (not `ls`) so a degenerate fixture (zero matches) reports a clean
# FAIL instead of aborting under set -euo pipefail. Mini #1445 v6.
PRE_NOTE_COUNT=$(find "$FIXTURE_WS/notes" -maxdepth 1 -name 'note-*.md' 2>/dev/null | wc -l | tr -d ' ')
out_pull_trip=$(run_sync --pull-only 2>&1 || true)
case "$out_pull_trip" in
  *"REFUSING pull"*|*"tripwire"*|*"deleted"*)
    echo "  OK: pull-side tripwire fired on peer mass-deletion"; pass=$((pass+1)) ;;
  *) echo "  FAIL: pull-side tripwire did NOT fire on peer's 60-file deletion: $out_pull_trip"; fail=$((fail+1)) ;;
esac

POST_NOTE_COUNT=$(find "$FIXTURE_WS/notes" -maxdepth 1 -name 'note-*.md' 2>/dev/null | wc -l | tr -d ' ')
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
             env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --migrate-from-legacy --dry-run 2>&1 || true)
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
SUTANDO_FORCE_SYNC=1 env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --push-only 2>&1 | head -3
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
RESTORED_COUNT=$(find "$FIXTURE_WS/notes" -maxdepth 1 -name 'note-*.md' 2>/dev/null | wc -l | tr -d ' ')
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

# Mini #1445 v4+v6 test gap: assert HEAD restored to pre-pull SHA across
# N=10 repeated pull attempts (proves idempotency, not just one re-fire).
PRE_HEAD2=$(cd "$FIXTURE_WS" && git rev-parse HEAD 2>/dev/null)
N_ITER=10
drift_count=0
for i in $(seq 1 "$N_ITER"); do
    run_sync --pull-only >/dev/null 2>&1 || true
    cur_head=$(cd "$FIXTURE_WS" && git rev-parse HEAD 2>/dev/null)
    [ "$cur_head" = "$PRE_HEAD2" ] || drift_count=$((drift_count + 1))
done
if [ "$drift_count" = "0" ]; then
  echo "  OK: HEAD unchanged across $N_ITER repeated tripwire pulls ($PRE_HEAD2)"; pass=$((pass+1))
else
  echo "  FAIL: HEAD drifted in $drift_count of $N_ITER pulls"; fail=$((fail+1))
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
echo "==== Test 13: .env SUTANDO_MEMORY_REPO legacy alias (PR-2 — deprecated but honored) ===="
# PR-2 dropped SUTANDO_VAULT env-var support. The .env legacy alias
# SUTANDO_MEMORY_REPO is still warn-and-honored for one release. Test:
# write .env with SUTANDO_MEMORY_REPO + no --vault-url + no config field →
# script resolves vault from .env legacy + prints deprecation warning.

echo "SUTANDO_MEMORY_REPO=$FIXTURE_VAULT" > "$FIXTURE_REPO/.env"

out_legacy_alias=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --status 2>&1; echo "EXIT=$?")
legacy_exit=$(printf '%s' "$out_legacy_alias" | sed -n 's/^EXIT=//p' | tail -1)

if [ "$legacy_exit" = "0" ]; then
  echo "  OK: --status exits 0 with only SUTANDO_MEMORY_REPO in .env"; pass=$((pass+1))
else
  echo "  FAIL: --status exited $legacy_exit on legacy-alias .env: $out_legacy_alias"; fail=$((fail+1))
fi

case "$out_legacy_alias" in
  *"SUTANDO_MEMORY_REPO is deprecated"*)
    echo "  OK: legacy-alias deprecation warning surfaced"; pass=$((pass+1)) ;;
  *) echo "  FAIL: legacy-alias deprecation warning missing: $out_legacy_alias"; fail=$((fail+1)) ;;
esac

case "$out_legacy_alias" in
  *"$FIXTURE_VAULT"*)
    echo "  OK: vault URL resolved from .env legacy alias"; pass=$((pass+1)) ;;
  *) echo "  FAIL: vault URL not resolved from legacy .env: $out_legacy_alias"; fail=$((fail+1)) ;;
esac

rm -f "$FIXTURE_REPO/.env"

# ============================================================================
echo
echo "==== Test 14: --vault-url CLI flag (PR-2 — canonical explicit) ===="
# Run --status WITHOUT any .env, WITHOUT SUTANDO_VAULT (removed in PR-2),
# WITHOUT vault.remote_url in config — only --vault-url flag. Should resolve.

out_flag=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --vault-url "$FIXTURE_VAULT" --status 2>&1; echo "EXIT=$?")
flag_exit=$(printf '%s' "$out_flag" | sed -n 's/^EXIT=//p' | tail -1)

if [ "$flag_exit" = "0" ]; then
  echo "  OK: --status exits 0 with --vault-url flag"; pass=$((pass+1))
else
  echo "  FAIL: --status exited $flag_exit on --vault-url: $out_flag"; fail=$((fail+1))
fi

case "$out_flag" in
  *"$FIXTURE_VAULT"*)
    echo "  OK: vault URL resolved from --vault-url flag"; pass=$((pass+1)) ;;
  *) echo "  FAIL: vault URL not resolved from flag: $out_flag"; fail=$((fail+1)) ;;
esac

# Flag must NOT trigger legacy-alias deprecation warning
case "$out_flag" in
  *"SUTANDO_MEMORY_REPO is deprecated"*)
    echo "  FAIL: --vault-url spuriously triggered legacy-alias warning"; fail=$((fail+1)) ;;
  *)
    echo "  OK: --vault-url does NOT trigger legacy-alias warning"; pass=$((pass+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 15: vault.remote_url from sutando.config.local.json (PR-2 — canonical config) ===="
# Write vault.remote_url into sutando.config.local.json + run with NO --vault-url
# flag + NO .env → should resolve via config file (the recommended path).

cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"vault": {"remote_url": "$FIXTURE_VAULT"}}
JSON

out_config=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --status 2>&1; echo "EXIT=$?")
config_exit=$(printf '%s' "$out_config" | sed -n 's/^EXIT=//p' | tail -1)

if [ "$config_exit" = "0" ]; then
  echo "  OK: --status exits 0 with vault.remote_url from local config"; pass=$((pass+1))
else
  echo "  FAIL: --status exited $config_exit on config-driven vault: $out_config"; fail=$((fail+1))
fi

case "$out_config" in
  *"$FIXTURE_VAULT"*)
    echo "  OK: vault URL resolved from sutando.config.local.json"; pass=$((pass+1)) ;;
  *) echo "  FAIL: vault URL not resolved from config: $out_config"; fail=$((fail+1)) ;;
esac

# Config-driven path must NOT trigger legacy deprecation warning
case "$out_config" in
  *"SUTANDO_MEMORY_REPO is deprecated"*)
    echo "  FAIL: config-driven path spuriously triggered legacy warning"; fail=$((fail+1)) ;;
  *)
    echo "  OK: config-driven path does NOT trigger legacy warning"; pass=$((pass+1)) ;;
esac

# Cleanup: restore original empty local config
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 16: SUTANDO_VAULT env var is NO LONGER honored (PR-2 — removed) ===="
# Pre-PR-2, SUTANDO_VAULT was the canonical env var. PR-2 removed it because
# config-file + --vault-url cover both canonical and override cases. Setting
# the env var alone (no flag, no config, no .env) should NOT resolve vault.

out_removed=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_VAULT="$FIXTURE_VAULT" \
    bash "$SYNC" --status 2>&1; echo "EXIT=$?")

# Status should still exit 0 (it doesn't require vault to be resolved),
# but VAULT_URL must NOT match $FIXTURE_VAULT
case "$out_removed" in
  *"$FIXTURE_VAULT"*)
    echo "  FAIL: SUTANDO_VAULT env var was still honored (should be removed)"; fail=$((fail+1)) ;;
  *)
    echo "  OK: SUTANDO_VAULT env var ignored as expected"; pass=$((pass+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 17: --vault-url priority over legacy .env (PR-2) ===="
# Set BOTH --vault-url AND .env SUTANDO_MEMORY_REPO. Flag must win.

OTHER_VAULT="$TEST_ROOT/other-vault.git"
git init -q --bare "$OTHER_VAULT"
echo "SUTANDO_MEMORY_REPO=$OTHER_VAULT" > "$FIXTURE_REPO/.env"

out_pri=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --vault-url "$FIXTURE_VAULT" --status 2>&1)

# Flag value must be present
case "$out_pri" in
  *"$FIXTURE_VAULT"*)
    echo "  OK: --vault-url wins over .env legacy"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --vault-url did not win over .env: $out_pri"; fail=$((fail+1)) ;;
esac

# .env value must NOT be the resolved vault (flag wins). Check that the
# OTHER_VAULT path doesn't appear in the VAULT_URL line specifically — it may
# appear elsewhere in status output but not as the resolved value.
if printf '%s' "$out_pri" | grep -E "^VAULT_URL:" | grep -qF "$OTHER_VAULT"; then
  echo "  FAIL: .env value used as VAULT_URL despite --vault-url flag"; fail=$((fail+1))
else
  echo "  OK: .env legacy value NOT picked when --vault-url present"; pass=$((pass+1))
fi

rm -f "$FIXTURE_REPO/.env"

# ============================================================================
echo
echo "==== Test 18: sync-memory.sh deprecation banner (PR-2) ===="
# sync-memory.sh stays functional but emits a one-line deprecation banner
# pointing at sync-workspace.sh.

# Copy the (real, modified) sync-memory.sh into the fixture
cp "$REPO/scripts/sync-memory.sh" "$FIXTURE_REPO/scripts/"
SYNC_MEM="$FIXTURE_REPO/scripts/sync-memory.sh"

# Invoke with a flag that triggers early exit (e.g. SUTANDO_MEMORY_REPO unset
# → script bails). We just want to see if the banner emits.
out_banner=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    bash "$SYNC_MEM" 2>&1 || true)

case "$out_banner" in
  *"DEPRECATED"*"sync-workspace.sh"*)
    echo "  OK: sync-memory.sh prints deprecation banner pointing at sync-workspace.sh"; pass=$((pass+1)) ;;
  *) echo "  FAIL: sync-memory.sh missing deprecation banner: $out_banner" | head -5; fail=$((fail+1)) ;;
esac

# Suppress flag should silence it
out_silent=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_SYNC_MEMORY_SUPPRESS_DEPRECATION=1 \
    bash "$SYNC_MEM" 2>&1 || true)

case "$out_silent" in
  *"DEPRECATED"*) echo "  FAIL: SUPPRESS flag did not silence deprecation banner"; fail=$((fail+1)) ;;
  *) echo "  OK: SUTANDO_SYNC_MEMORY_SUPPRESS_DEPRECATION=1 silences banner"; pass=$((pass+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 19: graceful fall-through when sutando-config.sh vault-url returns empty (Pro #1446) ===="
# Pro's hold flag: "is sutando-config.sh vault-url implemented, or does the
# helper silently return empty and fall through to deprecated .env?"
# This test stages the explicit fall-through path:
#   - config has NO vault.remote_url (helper returns empty)
#   - NO --vault-url flag
#   - .env HAS SUTANDO_MEMORY_REPO
# Expected: script resolves URL via .env legacy + emits deprecation warning.
# Proves the silent-no-op DOES gracefully degrade (config helper empty → .env
# fallback fires, not a crash, not a loop).

# Verify the helper itself returns empty when no remote_url in config
helper_out="$(SUTANDO_REPO_DIR="$FIXTURE_REPO" \
              SUTANDO_WORKSPACE="$FIXTURE_WS" \
              SUTANDO_TEST_MODE=1 \
              bash "$FIXTURE_REPO/scripts/sutando-config.sh" vault-url 2>/dev/null)"
if [ -z "$helper_out" ]; then
  echo "  OK: sutando-config.sh vault-url returns empty when config has no remote_url"; pass=$((pass+1))
else
  echo "  FAIL: helper returned non-empty for config-without-remote_url: $helper_out"; fail=$((fail+1))
fi

# Stage the fall-through path: config empty + .env has legacy alias
echo "SUTANDO_MEMORY_REPO=$FIXTURE_VAULT" > "$FIXTURE_REPO/.env"

out_fallthru=$(env \
    SUTANDO_REPO_DIR="$FIXTURE_REPO" \
    SUTANDO_WORKSPACE="$FIXTURE_WS" \
    SUTANDO_TEST_MODE=1 \
    bash "$SYNC" --status 2>&1; echo "EXIT=$?")
fallthru_exit=$(printf '%s' "$out_fallthru" | sed -n 's/^EXIT=//p' | tail -1)

if [ "$fallthru_exit" = "0" ]; then
  echo "  OK: --status exits 0 with empty config + legacy .env (graceful fall-through)"; pass=$((pass+1))
else
  echo "  FAIL: --status crashed/exited $fallthru_exit on fall-through: $out_fallthru"; fail=$((fail+1))
fi

case "$out_fallthru" in
  *"SUTANDO_MEMORY_REPO is deprecated"*)
    echo "  OK: deprecation warning fired (correct fall-through path used)"; pass=$((pass+1)) ;;
  *) echo "  FAIL: no deprecation warning on fall-through — config-path might be silently winning"; fail=$((fail+1)) ;;
esac

case "$out_fallthru" in
  *"$FIXTURE_VAULT"*)
    echo "  OK: vault URL resolved via .env legacy after empty config"; pass=$((pass+1)) ;;
  *) echo "  FAIL: vault URL not resolved at all: $out_fallthru"; fail=$((fail+1)) ;;
esac

rm -f "$FIXTURE_REPO/.env"

# ============================================================================
echo
echo "==== Test 20: vault.sync.include from config drives .gitignore (PR-3) ===="
# Verify that what ends up in .gitignore comes from sutando.config.json's
# vault.sync.include (not a hardcoded list). Semantics: local config REPLACES
# tracked default's sync.include array (standard JSON merge; arrays don't
# concat). Users who want to keep defaults + add their own path must include
# the defaults explicitly. PR-3 design.

# Reset workspace state so --init can re-run cleanly
rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"

# Write a config-local with defaults PLUS one custom path (REPLACE semantics)
cat > "$FIXTURE_REPO/sutando.config.local.json" <<'JSON'
{
  "vault": {
    "sync": {
      "include": [
        "notes/",
        "pending-questions.md",
        "build_log.md",
        ".claude-sutando/projects/*/memory/",
        "custom-dir/"
      ]
    }
  }
}
JSON

# --init will regenerate .gitignore from config
run_sync --init 2>&1 | head -5 >/dev/null || true

# All paths present (use grep — case-pattern with whitespace is too strict)
all_present=1
for needle in "!notes/" "!pending-questions.md" "!build_log.md" "!.claude-sutando/projects/" "!custom-dir/"; do
  grep -qF "$needle" "$FIXTURE_WS/.gitignore" || all_present=0
done
if [ "$all_present" = "1" ]; then
  echo "  OK: defaults + custom include all emitted to .gitignore"; pass=$((pass+1))
else
  echo "  FAIL: some include paths missing"; grep -E "^!" "$FIXTURE_WS/.gitignore" | head -10; fail=$((fail+1))
fi

# Custom include from local config appears
if grep -qF "!custom-dir/" "$FIXTURE_WS/.gitignore"; then
  echo "  OK: custom include 'custom-dir/' from sutando.config.local.json present"; pass=$((pass+1))
else
  echo "  FAIL: custom include NOT in .gitignore"; fail=$((fail+1))
fi

# Reset config-local
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 21: vault.sync.exclude carves out from include (PR-3) ===="
# An exclude should override an otherwise-included parent. Write include for
# `data/` + exclude for `data/secret/` → `data/secret/` gitignored,
# `data/foo.md` visible to git. Use `git check-ignore` to ask git directly
# (untracked-vs-ignored is clearer than parsing `git status` output).

cat > "$FIXTURE_REPO/sutando.config.local.json" <<'JSON'
{
  "vault": {
    "sync": {
      "include": ["data/"],
      "exclude": ["data/secret/"]
    }
  }
}
JSON

rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"
run_sync --init 2>&1 | head -5 >/dev/null || true

GI="$FIXTURE_WS/.gitignore"
if grep -qF "!data/" "$GI" && grep -qE "^data/secret/" "$GI"; then
  echo "  OK: include + exclude both emitted to .gitignore"; pass=$((pass+1))
else
  echo "  FAIL: include/exclude combination missing"; grep -E "^!?data" "$GI" | head; fail=$((fail+1))
fi

# Create test files
mkdir -p "$FIXTURE_WS/data/secret"
echo "ok" > "$FIXTURE_WS/data/foo.md"
echo "shh" > "$FIXTURE_WS/data/secret/key.txt"

# Ask git directly which files are ignored. Exit 0 = ignored, 1 = NOT ignored.
cd "$FIXTURE_WS"
if git check-ignore -q data/foo.md 2>/dev/null; then
  foo_ignored=1
else
  foo_ignored=0
fi
if git check-ignore -q data/secret/key.txt 2>/dev/null; then
  secret_ignored=1
else
  secret_ignored=0
fi
cd - >/dev/null

if [ "$foo_ignored" = "0" ]; then
  echo "  OK: data/foo.md NOT ignored (visible to git)"; pass=$((pass+1))
else
  echo "  FAIL: data/foo.md was ignored — exclude carve-out blocked the include"; fail=$((fail+1))
fi

if [ "$secret_ignored" = "1" ]; then
  echo "  OK: data/secret/key.txt IS ignored (exclude carved it out)"; pass=$((pass+1))
else
  echo "  FAIL: data/secret/key.txt NOT ignored — exclude didn't carve out"; fail=$((fail+1))
fi

rm -rf "$FIXTURE_WS/data"
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 22: ancestor-chain un-ignore for nested include path (PR-3) ===="
# Verify _emit_include_lines emits the FULL ancestor chain — gitignore can't
# include a child whose ancestor is excluded by `*`. For include `a/b/c/`,
# expect `!a/`, `!a/b/`, `!a/b/c/`, `!a/b/c/**`.

cat > "$FIXTURE_REPO/sutando.config.local.json" <<'JSON'
{
  "vault": {
    "sync": {
      "include": ["a/b/c/"]
    }
  }
}
JSON

rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"
run_sync --init 2>&1 | head -5 >/dev/null || true

GI="$FIXTURE_WS/.gitignore"
chain_ok=1
for needle in "!a/" "!a/b/" "!a/b/c/" "!a/b/c/**"; do
  grep -qF "$needle" "$GI" || chain_ok=0
done

if [ "$chain_ok" = "1" ]; then
  echo "  OK: ancestor chain emitted for nested include (!a/, !a/b/, !a/b/c/, !a/b/c/**)"; pass=$((pass+1))
else
  echo "  FAIL: ancestor chain incomplete for a/b/c/:"; grep -E "^!a" "$GI" | head; fail=$((fail+1))
fi

# Reset
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
