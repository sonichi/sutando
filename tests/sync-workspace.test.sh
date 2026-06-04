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
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
