#!/usr/bin/env bash
# Integration tests for scripts/sync-workspace.sh — PR-1 (Phase 4).
#
# Sets up an isolated fixture workspace + a local bare repo as the vault,
# then exercises --init / --push-only / --pull-only / mass-deletion-tripwire
# / migration. Hermetic: never touches the operator's real workspace or vault.
#
# Test isolation strategy:
#   - SUTANDO_WORKSPACE + SUTANDO_TEST_MODE=1 → M0 helper honors the env
#     (the v0.8 escape hatch added in #1440 for exactly this purpose)
#   - SUTANDO_REPO_DIR → fake sutando-checkout with CLAUDE.md+skills+.git
#     (so the script's auto-detect picks up the fixture, and local_slug
#     derives from the fixture path not the real repo)
#   - SUTANDO_VAULT → local bare repo URL (no network)
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

# ---- Fixture setup ----
# Fake sutando-checkout (gives the script a REPO_DIR with the M0 helper)
FIXTURE_REPO="$TEST_ROOT/sutando"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src" "$FIXTURE_REPO/skills"
touch "$FIXTURE_REPO/CLAUDE.md"
git init -q "$FIXTURE_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
# Minimal sutando.config.json so the loader doesn't error
cat > "$FIXTURE_REPO/sutando.config.json" <<JSON
{"workspace": {"path": "\${REPO_DIR}/workspace"}, "vault": {"enabled": false}}
JSON

# Fixture workspace — realpath'd to match what the script outputs on macOS
# (where /var/folders/... → /private/var/folders/... via the standard tmpdir
# symlink). Use the realpath form in assertions so the test runs on both
# macOS and Linux without divergence.
FIXTURE_WS_RAW="$TEST_ROOT/workspace"
mkdir -p "$FIXTURE_WS_RAW"
if command -v realpath >/dev/null 2>&1; then
  FIXTURE_WS="$(realpath "$FIXTURE_WS_RAW")"
else
  FIXTURE_WS="$FIXTURE_WS_RAW"
fi

# Bare repo as the vault (local, no network)
FIXTURE_VAULT="$TEST_ROOT/vault.git"
git init -q --bare "$FIXTURE_VAULT"

# Common env for invocations
COMMON_ENV=(
  SUTANDO_REPO_DIR="$FIXTURE_REPO"
  SUTANDO_WORKSPACE="$FIXTURE_WS"
  SUTANDO_TEST_MODE=1
  SUTANDO_VAULT="$FIXTURE_VAULT"
)

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"

run_sync() {
  env "${COMMON_ENV[@]}" bash "$SYNC" "$@"
}

# ============================================================================
echo "==== Test 1: --status before init (env resolution + map.json absent) ===="
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
  *"canonical_id:  "*)
    echo "  OK: --status shows canonical_id"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --status canonical_id missing"; fail=$((fail+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 2: --init creates .gitignore, projects.map.json, canonical memory dir, first commit + push ===="
run_sync --init 2>&1 | head -10

assert_dir_exists ".git exists in workspace"        "$FIXTURE_WS/.git"
assert_file_exists ".gitignore created"             "$FIXTURE_WS/.gitignore"
assert_file_exists "projects.map.json created"      "$FIXTURE_WS/.sutando-vault/projects.map.json"

# canonical_id = sha256-8 of vault URL (mirror script's derivation)
CANONICAL=$(printf '%s' "$FIXTURE_VAULT" | shasum -a 256 | cut -c1-8)
assert_dir_exists "canonical memory dir created"    "$FIXTURE_WS/.claude-sutando/projects/${CANONICAL}/memory"

# .gitignore content sanity
assert_contains ".gitignore whitelists notes/"  "!notes/"  "$FIXTURE_WS/.gitignore"
assert_contains ".gitignore whitelists canonical memory mirror"  "$CANONICAL"  "$FIXTURE_WS/.gitignore"
assert_contains ".gitignore hard-denies .env"   ".env*"    "$FIXTURE_WS/.gitignore"

# Vault remote configured
REMOTE_URL=$(cd "$FIXTURE_WS" && git remote get-url origin)
assert_eq "git remote origin = vault"  "$FIXTURE_VAULT"  "$REMOTE_URL"

# host/<hostname> branch exists in bare repo
HOST=$(hostname | sed 's/\..*//')
if git --git-dir="$FIXTURE_VAULT" rev-parse "refs/heads/host/${HOST}" >/dev/null 2>&1; then
  echo "  OK: host/${HOST} branch pushed to vault"; pass=$((pass+1))
else
  echo "  FAIL: host/${HOST} branch NOT in vault"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 3: idempotent re-init (no error, no duplicate work) ===="
out_reinit=$(run_sync --init 2>&1)
if echo "$out_reinit" | grep -q "already a git repo"; then
  echo "  OK: re-init detects existing repo"; pass=$((pass+1))
else
  echo "  INFO: re-init output: $out_reinit"  # not strictly required
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
echo "==== Test 5: translation layer — write to local-slug, push, verify in canonical + vault ===="
# local_slug = REPO_DIR with / replaced by - (mirror the script's derivation)
LOCAL_SLUG_DIR="$FIXTURE_WS/.claude-sutando/projects/$(printf '%s' "$FIXTURE_REPO" | sed 's|/|-|g')/memory"
mkdir -p "$LOCAL_SLUG_DIR"
echo "test memory content from $HOST" > "$LOCAL_SLUG_DIR/feedback_test.md"

run_sync --push-only 2>&1 | head -5

CANONICAL_MEM="$FIXTURE_WS/.claude-sutando/projects/${CANONICAL}/memory/feedback_test.md"
assert_file_exists "memory copied local-slug → canonical" "$CANONICAL_MEM"

# Verify it's in the vault remote
if git --git-dir="$FIXTURE_VAULT" show "refs/heads/host/${HOST}:.claude-sutando/projects/${CANONICAL}/memory/feedback_test.md" >/dev/null 2>&1; then
  echo "  OK: feedback_test.md present in vault host/${HOST} branch"; pass=$((pass+1))
else
  echo "  FAIL: feedback_test.md NOT in vault"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 6: --pull-only — simulate peer write, pull, verify canonical → local-slug ===="
# Simulate a peer host by cloning the bare repo to a second workspace,
# basing the peer branch off host 1's existing branch (bare repo has no
# default HEAD → checkout the existing host branch explicitly first).
PEER_WS="$TEST_ROOT/peer-workspace"
git clone -q "$FIXTURE_VAULT" "$PEER_WS" 2>/dev/null
(
    cd "$PEER_WS"
    git checkout -B "host/peerhost" "origin/host/${HOST}" >/dev/null 2>&1
    mkdir -p ".claude-sutando/projects/${CANONICAL}/memory"
    echo "from peer" > ".claude-sutando/projects/${CANONICAL}/memory/feedback_peer.md"
    git add -A
    git -c user.email=peer@test -c user.name=peer commit -q -m "peer write" >/dev/null 2>&1
    git push -q origin "host/peerhost" 2>/dev/null
)

# Now pull on host 1
run_sync --pull-only 2>&1 | head -5

# Verify the peer's write landed in our local-slug
LOCAL_PEER_FILE="$LOCAL_SLUG_DIR/feedback_peer.md"
assert_file_exists "peer's memory copied canonical → local-slug" "$LOCAL_PEER_FILE"

# ============================================================================
echo
echo "==== Test 7: mass-deletion tripwire ===="
# Stage a deletion of >50 files to trigger the tripwire. Create then delete them.
cd "$FIXTURE_WS"
mkdir -p notes
for i in $(seq 1 60); do echo "n$i" > "notes/note-$i.md"; done
git add notes/
git -c user.email=test@test -c user.name=test commit -q -m "60 notes"
# Delete them all
rm -f notes/note-*.md
cd - >/dev/null

out_tripwire=$(run_sync --push-only 2>&1 || true)
case "$out_tripwire" in
  *"refusing push"*|*"tripwire"*|*"would delete"*)
    echo "  OK: mass-deletion tripwire fired"; pass=$((pass+1)) ;;
  *) echo "  FAIL: tripwire didn't fire on 60 deletions: $out_tripwire"; fail=$((fail+1)) ;;
esac

# Verify nothing was actually pushed (working tree reset)
cd "$FIXTURE_WS"
if [ -z "$(git diff --cached --name-only)" ]; then
  echo "  OK: tripwire reset staged changes"; pass=$((pass+1))
else
  echo "  FAIL: tripwire didn't reset staged changes"; fail=$((fail+1))
fi
cd - >/dev/null

# Restore for any further tests
for i in $(seq 1 60); do echo "n$i" > "$FIXTURE_WS/notes/note-$i.md"; done

# ============================================================================
echo
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
