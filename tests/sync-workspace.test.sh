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
  SUTANDO_WS_ID_OVERRIDE=t01ws1
)

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
HOST=$(hostname | sed 's/\..*//')
# Post-wsId branch shape (`host/<hostname>/<wsId>`). WS_ID matches the override
# set in COMMON_ENV above, so test assertions know the exact ref.
WS_ID=t01ws1
HOST_BRANCH="refs/heads/host/${HOST}/${WS_ID}"

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
echo "==== Test 2: --init creates .git/info/exclude + .git + first push ===="
run_sync --init 2>&1 | head -10

assert_dir_exists ".git exists in workspace"  "$FIXTURE_WS/.git"
assert_file_exists ".git/info/exclude created"  "$FIXTURE_WS/.git/info/exclude"

# Post-leak-fix: carrier-set whitelist (deny-all + un-ignore allowlist +
# hard-deny credentials) lives at .git/info/exclude — inside .git/ which
# the OUTER sutando repo treats as opaque. Identical un-ignore (`!notes/`)
# rules in .git/info/exclude cannot leak across the inner/outer boundary
# the way an in-tree workspace/.gitignore did (2026-06-04 leak repro).
EXCLUDE_FILE="$FIXTURE_WS/.git/info/exclude"

# No in-tree workspace/.gitignore — that file was the leak source. Sync
# engine deletes it on init if a legacy one exists.
if [ -f "$FIXTURE_WS/.gitignore" ]; then
  echo "  FAIL: workspace/.gitignore exists — option (6) requires rules in .git/info/exclude only"; fail=$((fail+1))
else
  echo "  OK: no in-tree workspace/.gitignore (leak source removed)"; pass=$((pass+1))
fi

# Exclude file declares deny-all + un-ignore allowlist.
if grep -qE '^\*$' "$EXCLUDE_FILE"; then
  echo "  OK: .git/info/exclude declares deny-all (\`*\`) at top level"; pass=$((pass+1))
else
  echo "  FAIL: .git/info/exclude missing top-level \`*\` deny-all"; fail=$((fail+1))
fi
if grep -qE '^!notes/' "$EXCLUDE_FILE"; then
  echo "  OK: .git/info/exclude un-ignores carrier set (e.g. !notes/)"; pass=$((pass+1))
else
  echo "  FAIL: .git/info/exclude missing carrier-set un-ignore rules"; fail=$((fail+1))
fi

# M3: secret material hard-deny (SSH private keys + cert/key extensions).
for _deny in "id_rsa" "id_ed25519" "*.pem" "*.key" "*.p12"; do
  if grep -qxF "$_deny" "$EXCLUDE_FILE"; then
    echo "  OK: .git/info/exclude hard-denies secret pattern '$_deny' (M3)"; pass=$((pass+1))
  else
    echo "  FAIL: .git/info/exclude missing secret deny '$_deny' (M3)"; fail=$((fail+1))
  fi
done
# Public keys must remain syncable — *.pub is NOT denied.
if grep -qxF '*.pub' "$EXCLUDE_FILE"; then
  echo "  FAIL: .git/info/exclude wrongly denies *.pub (public keys should sync)"; fail=$((fail+1))
else
  echo "  OK: .git/info/exclude does not deny *.pub (public keys still sync)"; pass=$((pass+1))
fi

# Verify the OLD canonical-specific pattern is GONE
if grep -qE 'projects/[a-f0-9]{8}/memory' "$EXCLUDE_FILE"; then
  echo "  FAIL: .git/info/exclude still has canonical-id-specific pattern"; fail=$((fail+1))
else
  echo "  OK: .git/info/exclude has no canonical-id-specific pattern"; pass=$((pass+1))
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
  echo "  OK: host/${HOST}/${WS_ID} branch pushed to vault"; pass=$((pass+1))
else
  echo "  FAIL: host/${HOST}/${WS_ID} branch NOT in vault"; fail=$((fail+1))
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
    git checkout -B "host/peerhost" "origin/host/${HOST}/${WS_ID}" >/dev/null 2>&1
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
echo "==== Test 9: .git/info/exclude overwrite warning (Pro #1445 review fix #3) ===="
# Modify .git/info/exclude in place with a non-comment line, then run
# --init without --force-gitignore. Expected: refuse + print diff.
# (Stock-comments-only exclude file is auto-overwritten without prompt;
# the warning fires only when an operator added real content.)
echo "my-custom-rule.txt" >> "$FIXTURE_WS/.git/info/exclude"
out_overwrite=$(run_sync --init 2>&1 || true)
case "$out_overwrite" in
  *"Refusing to overwrite"*)
    echo "  OK: --init refuses to overwrite user-edited .git/info/exclude"; pass=$((pass+1)) ;;
  *) echo "  FAIL: --init silently overwrote user-edited .git/info/exclude: $out_overwrite"; fail=$((fail+1)) ;;
esac
# Verify the user's edit survived
if grep -q "my-custom-rule.txt" "$FIXTURE_WS/.git/info/exclude"; then
  echo "  OK: user's custom exclude line preserved (not overwritten)"; pass=$((pass+1))
else
  echo "  FAIL: user's custom exclude line was lost"; fail=$((fail+1))
fi

# Now with --force-gitignore → should overwrite (flag retains its name
# for back-compat with the pre-(6) in-tree .gitignore world).
out_force=$(env "${COMMON_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --init --force-gitignore 2>&1 || true)
if grep -q "my-custom-rule.txt" "$FIXTURE_WS/.git/info/exclude"; then
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
    git checkout -B "host/peerhost2" "origin/host/${HOST}/${WS_ID}" >/dev/null 2>&1
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

# I1: per-host destinations are hostname-qualified under hosts/<host>/.
if grep -qE 'hosts/[^/]+/build_log\.md' <<<"$out_dryrun"; then
  echo "  OK: --migrate targets hosts/<host>/build_log.md (I1, per-host)"; pass=$((pass+1))
else
  echo "  FAIL: --migrate build_log not targeted at hosts/<host>/ (I1): $out_dryrun"; fail=$((fail+1))
fi
if grep -qE 'hosts/[^/]+/pending-questions\.md' <<<"$out_dryrun"; then
  echo "  OK: --migrate targets hosts/<host>/pending-questions.md (I1, per-host)"; pass=$((pass+1))
else
  echo "  FAIL: --migrate pending-questions not targeted at hosts/<host>/ (I1): $out_dryrun"; fail=$((fail+1))
fi
# Old interim layout must be gone — no build_log/<host>.md dir-split, no root pending-questions.
if grep -qE 'build_log/[^/]+\.md' <<<"$out_dryrun"; then
  echo "  FAIL: --migrate still uses interim build_log/<host>.md layout (I1 regression)"; fail=$((fail+1))
else
  echo "  OK: --migrate no longer uses interim build_log/<host>.md layout (I1)"; pass=$((pass+1))
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
    git checkout -B "host/peerhost3" "origin/host/${HOST}/${WS_ID}" >/dev/null 2>&1
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
echo "==== Test 20: vault.sync.include from config drives carrier set (PR-3 + leak fix) ===="
# Post-leak-fix: vault.sync.include drives the un-ignore allowlist baked
# into .git/info/exclude (per-clone, opaque to outer sutando repo). Verify
# behavior end-to-end: files under each include path get pushed to vault.

rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"
rm -rf "$FIXTURE_VAULT" && git init -q --bare "$FIXTURE_VAULT"

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

# Create files under each include path
mkdir -p "$FIXTURE_WS/notes" "$FIXTURE_WS/custom-dir"
echo "n" > "$FIXTURE_WS/notes/n.md"
echo "c" > "$FIXTURE_WS/custom-dir/c.md"
echo "bl" > "$FIXTURE_WS/build_log.md"
echo "pq" > "$FIXTURE_WS/pending-questions.md"

run_sync --init 2>&1 | head -5 >/dev/null || true

# All carrier-path files must be in the vault under the host branch
all_in_vault=1
for path in "notes/n.md" "custom-dir/c.md" "build_log.md" "pending-questions.md"; do
  git --git-dir="$FIXTURE_VAULT" show "${HOST_BRANCH}:${path}" >/dev/null 2>&1 || all_in_vault=0
done
if [ "$all_in_vault" = "1" ]; then
  echo "  OK: defaults + custom include all pushed to vault"; pass=$((pass+1))
else
  echo "  FAIL: some carrier files missing from vault"; fail=$((fail+1))
fi

if git --git-dir="$FIXTURE_VAULT" show "${HOST_BRANCH}:custom-dir/c.md" >/dev/null 2>&1; then
  echo "  OK: custom include 'custom-dir/' from sutando.config.local.json pushed"; pass=$((pass+1))
else
  echo "  FAIL: custom include NOT pushed"; fail=$((fail+1))
fi

rm -rf "$FIXTURE_WS/notes" "$FIXTURE_WS/custom-dir" "$FIXTURE_WS/build_log.md" "$FIXTURE_WS/pending-questions.md"
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 21: vault.sync.exclude carves out from include (PR-3 + leak fix) ===="
# Post-leak-fix: exclude is emitted into .git/info/exclude AFTER the
# include un-ignores so gitignore last-match wins on the carve-out path.
# Verify behavioral end: data/foo.md IS in vault, while data/secret/key.txt
# is NOT.

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
rm -rf "$FIXTURE_VAULT" && git init -q --bare "$FIXTURE_VAULT"
mkdir -p "$FIXTURE_WS/data/secret"
echo "ok" > "$FIXTURE_WS/data/foo.md"
echo "shh" > "$FIXTURE_WS/data/secret/key.txt"

run_sync --init 2>&1 | head -5 >/dev/null || true

if git --git-dir="$FIXTURE_VAULT" show "${HOST_BRANCH}:data/foo.md" >/dev/null 2>&1; then
  echo "  OK: data/foo.md pushed (included via 'data/')"; pass=$((pass+1))
else
  echo "  FAIL: data/foo.md NOT pushed"; fail=$((fail+1))
fi

if git --git-dir="$FIXTURE_VAULT" show "${HOST_BRANCH}:data/secret/key.txt" >/dev/null 2>&1; then
  echo "  FAIL: data/secret/key.txt was pushed (exclude didn't carve out)"; fail=$((fail+1))
else
  echo "  OK: data/secret/key.txt NOT pushed (exclude carved it out)"; pass=$((pass+1))
fi

rm -rf "$FIXTURE_WS/data"
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 22: nested include path is tracked end-to-end (PR-3 + leak fix) ===="
# Post-leak-fix: ancestor-chain un-ignore rules live in .git/info/exclude
# (opaque to outer sutando repo) so a nested include like `a/b/c/` walks
# its ancestors safely. Verify behavioral end: a deeply-nested include
# results in tracked files in the vault.

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
rm -rf "$FIXTURE_VAULT" && git init -q --bare "$FIXTURE_VAULT"
mkdir -p "$FIXTURE_WS/a/b/c"
echo "nested" > "$FIXTURE_WS/a/b/c/leaf.md"

run_sync --init 2>&1 | head -5 >/dev/null || true

if git --git-dir="$FIXTURE_VAULT" show "${HOST_BRANCH}:a/b/c/leaf.md" >/dev/null 2>&1; then
  echo '  OK: nested a/b/c/leaf.md pushed (ancestor-chain un-ignore in .git/info/exclude)'; pass=$((pass+1))
else
  echo "  FAIL: nested file NOT pushed"; fail=$((fail+1))
fi

rm -rf "$FIXTURE_WS/a"
echo '{}' > "$FIXTURE_REPO/sutando.config.local.json"

# ============================================================================
echo
echo "==== Test 24: two workspaces on same host → distinct branches (wsId scheme) ===="
# When two Sutando installs run on the same machine (same hostname) but
# different workspaces (different paths), they MUST push to distinct
# vault branches. Before wsId: both would clobber `host/<hostname>`.
# After wsId: `host/<hostname>/<wsIdA>` vs `host/<hostname>/<wsIdB>`.
TEST24_VAULT="$TEST_ROOT/vault-24.git"
git init -q --bare "$TEST24_VAULT"

# Workspace A
WSA_WS="$TEST_ROOT/ws-A"
WSA_REPO="$TEST_ROOT/ws-A-repo"
mkdir -p "$WSA_WS" "$WSA_REPO/scripts" "$WSA_REPO/src"
touch "$WSA_REPO/CLAUDE.md"
git init -q "$WSA_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$WSA_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$WSA_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$WSA_REPO/src/"
cp "$FIXTURE_REPO/sutando.config.json" "$WSA_REPO/"
WSA_SLUG=$(printf '%s' "$WSA_REPO" | sed 's|/|-|g')
mkdir -p "$WSA_WS/.claude-sutando/projects/${WSA_SLUG}/memory"
mkdir -p "$WSA_WS/notes"
echo "from workspace A" > "$WSA_WS/.claude-sutando/projects/${WSA_SLUG}/memory/feedback_wsA.md"

env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$WSA_REPO" \
    SUTANDO_WORKSPACE="$WSA_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=samehost \
    SUTANDO_WS_ID_OVERRIDE=t24wsa \
    bash "$WSA_REPO/scripts/sync-workspace.sh" --vault-url "$TEST24_VAULT" --init 2>&1 | tail -3 >/dev/null

# Workspace B — SAME host (samehost) but different workspace path → different wsId
WSB_WS="$TEST_ROOT/ws-B"
WSB_REPO="$TEST_ROOT/ws-B-repo"
mkdir -p "$WSB_WS" "$WSB_REPO/scripts" "$WSB_REPO/src"
touch "$WSB_REPO/CLAUDE.md"
git init -q "$WSB_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$WSB_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$WSB_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$WSB_REPO/src/"
cp "$FIXTURE_REPO/sutando.config.json" "$WSB_REPO/"
WSB_SLUG=$(printf '%s' "$WSB_REPO" | sed 's|/|-|g')
mkdir -p "$WSB_WS/.claude-sutando/projects/${WSB_SLUG}/memory"
mkdir -p "$WSB_WS/notes"
echo "from workspace B" > "$WSB_WS/.claude-sutando/projects/${WSB_SLUG}/memory/feedback_wsB.md"

env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$WSB_REPO" \
    SUTANDO_WORKSPACE="$WSB_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=samehost \
    SUTANDO_WS_ID_OVERRIDE=t24wsb \
    bash "$WSB_REPO/scripts/sync-workspace.sh" --vault-url "$TEST24_VAULT" --init 2>&1 | tail -3 >/dev/null

# Both branches present in vault, side-by-side
WSA_SHA=$(git --git-dir="$TEST24_VAULT" rev-parse refs/heads/host/samehost/t24wsa 2>/dev/null || echo "")
WSB_SHA=$(git --git-dir="$TEST24_VAULT" rev-parse refs/heads/host/samehost/t24wsb 2>/dev/null || echo "")
if [ -n "$WSA_SHA" ] && [ -n "$WSB_SHA" ]; then
  echo "  OK: host/samehost/t24wsa AND host/samehost/t24wsb both present in vault"; pass=$((pass+1))
else
  echo "  FAIL: branches missing (A=$WSA_SHA B=$WSB_SHA)"; fail=$((fail+1))
fi

# WSA's content NOT in WSB's branch, and vice-versa (siloed)
if git --git-dir="$TEST24_VAULT" cat-file -e "refs/heads/host/samehost/t24wsa:.claude-sutando/projects/${WSA_SLUG}/memory/feedback_wsA.md" 2>/dev/null \
   && ! git --git-dir="$TEST24_VAULT" cat-file -e "refs/heads/host/samehost/t24wsa:.claude-sutando/projects/${WSB_SLUG}/memory/feedback_wsB.md" 2>/dev/null; then
  echo "  OK: wsA branch contains wsA's content and NOT wsB's"; pass=$((pass+1))
else
  echo "  FAIL: wsA branch siloing broken"; fail=$((fail+1))
fi

# Persistence: re-run --init on wsA without override → reads existing wsId from file
WSA_PERSISTED_ID=$(tr -d '[:space:]' < "$WSA_WS/.sutando-vault/ws-id" 2>/dev/null || echo "")
if [ "$WSA_PERSISTED_ID" = "t24wsa" ]; then
  echo "  OK: wsId t24wsa persisted to $WSA_WS/.sutando-vault/ws-id"; pass=$((pass+1))
else
  echo "  FAIL: wsId not persisted (got '$WSA_PERSISTED_ID', expected 't24wsa')"; fail=$((fail+1))
fi

# Re-run --init on wsA WITHOUT override → must NOT regenerate, reads persisted
RERUN_OUT=$(env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$WSA_REPO" \
    SUTANDO_WORKSPACE="$WSA_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=samehost \
    bash "$WSA_REPO/scripts/sync-workspace.sh" --vault-url "$TEST24_VAULT" --status 2>&1)
case "$RERUN_OUT" in
  *"WS_ID:         t24wsa"*)
    echo "  OK: --status reads persisted wsId without regenerating"; pass=$((pass+1)) ;;
  *)
    echo "  FAIL: --status did not surface persisted wsId. Output: $RERUN_OUT"; fail=$((fail+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 23: --pull-only handles unrelated histories across fresh hosts (Codex P1.3) ===="
# Two hosts that each run `--init` from scratch against the SAME bare vault
# produce TWO unrelated initial commits — no common ancestor. Pre-fix, the
# first cross-host `git merge` died with "refusing to merge unrelated
# histories" and `--pull-only` silently "succeeded" with 0 peers merged.
# This test sets up that exact topology and verifies the second host's
# pull-only succeeds AND surfaces the peer's content into the local
# workspace.
TEST23_VAULT="$TEST_ROOT/vault-23.git"
git init -q --bare "$TEST23_VAULT"

# Host A: fresh workspace + --init against the shared vault
HOSTA_WS="$TEST_ROOT/hostA-ws"
HOSTA_REPO="$TEST_ROOT/hostA-repo"
mkdir -p "$HOSTA_WS" "$HOSTA_REPO/scripts" "$HOSTA_REPO/src"
touch "$HOSTA_REPO/CLAUDE.md"
git init -q "$HOSTA_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$HOSTA_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$HOSTA_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$HOSTA_REPO/src/"
cp "$FIXTURE_REPO/sutando.config.json" "$HOSTA_REPO/"
HOSTA_SLUG=$(printf '%s' "$HOSTA_REPO" | sed 's|/|-|g')
mkdir -p "$HOSTA_WS/.claude-sutando/projects/${HOSTA_SLUG}/memory"
mkdir -p "$HOSTA_WS/notes"
echo "from hostA" > "$HOSTA_WS/.claude-sutando/projects/${HOSTA_SLUG}/memory/feedback_hostA.md"
echo "hostA note" > "$HOSTA_WS/notes/hostA-note.md"

# Run --init on host A — hostname is the same across both hosts (same machine
# running the test), so we override HOST per-invocation via the
# SUTANDO_HOST_OVERRIDE test-only shim in the script.
env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$HOSTA_REPO" \
    SUTANDO_WORKSPACE="$HOSTA_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=hostA \
    SUTANDO_WS_ID_OVERRIDE=t23wsa \
    bash "$HOSTA_REPO/scripts/sync-workspace.sh" --vault-url "$TEST23_VAULT" --init 2>&1 | tail -3 >/dev/null

# Host B: ALSO fresh, ALSO --init, ALSO pushes to the SAME vault — but with
# a DIFFERENT hostname → different host branch, independent root commit.
HOSTB_WS="$TEST_ROOT/hostB-ws"
HOSTB_REPO="$TEST_ROOT/hostB-repo"
mkdir -p "$HOSTB_WS" "$HOSTB_REPO/scripts" "$HOSTB_REPO/src"
touch "$HOSTB_REPO/CLAUDE.md"
git init -q "$HOSTB_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$HOSTB_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$HOSTB_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$HOSTB_REPO/src/"
cp "$FIXTURE_REPO/sutando.config.json" "$HOSTB_REPO/"
HOSTB_SLUG=$(printf '%s' "$HOSTB_REPO" | sed 's|/|-|g')
mkdir -p "$HOSTB_WS/.claude-sutando/projects/${HOSTB_SLUG}/memory"
mkdir -p "$HOSTB_WS/notes"
echo "from hostB" > "$HOSTB_WS/.claude-sutando/projects/${HOSTB_SLUG}/memory/feedback_hostB.md"
echo "hostB note" > "$HOSTB_WS/notes/hostB-note.md"

env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$HOSTB_REPO" \
    SUTANDO_WORKSPACE="$HOSTB_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=hostB \
    SUTANDO_WS_ID_OVERRIDE=t23wsb \
    bash "$HOSTB_REPO/scripts/sync-workspace.sh" --vault-url "$TEST23_VAULT" --init 2>&1 | tail -3 >/dev/null

# Verify two unrelated host branches exist in the vault (post-wsId shape)
HOSTA_SHA=$(git --git-dir="$TEST23_VAULT" rev-parse refs/heads/host/hostA/t23wsa 2>/dev/null || echo "")
HOSTB_SHA=$(git --git-dir="$TEST23_VAULT" rev-parse refs/heads/host/hostB/t23wsb 2>/dev/null || echo "")
if [ -n "$HOSTA_SHA" ] && [ -n "$HOSTB_SHA" ]; then
  echo "  OK: host/hostA/t23wsa and host/hostB/t23wsb both pushed to vault"; pass=$((pass+1))
else
  echo "  FAIL: one or both host branches missing from vault (A=$HOSTA_SHA B=$HOSTB_SHA)"; fail=$((fail+1))
fi

# Confirm unrelated histories (the pre-condition that triggered the bug)
MERGE_BASE=$(git --git-dir="$TEST23_VAULT" merge-base "$HOSTA_SHA" "$HOSTB_SHA" 2>/dev/null || echo "")
if [ -z "$MERGE_BASE" ]; then
  echo "  OK: hostA and hostB have NO common ancestor (unrelated histories — bug pre-condition)"; pass=$((pass+1))
else
  echo "  FAIL: hostA and hostB share ancestor $MERGE_BASE — test setup wrong"; fail=$((fail+1))
fi

# Now run --pull-only on host B → should merge hostA in via --allow-unrelated-histories
PULL_OUT=$(env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$HOSTB_REPO" \
    SUTANDO_WORKSPACE="$HOSTB_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=hostB \
    SUTANDO_WS_ID_OVERRIDE=t23wsb \
    bash "$HOSTB_REPO/scripts/sync-workspace.sh" --vault-url "$TEST23_VAULT" --pull-only 2>&1)

# Verify hostA's content surfaced into hostB's workspace
HOSTA_VISIBLE_FILE="$HOSTB_WS/.claude-sutando/projects/${HOSTA_SLUG}/memory/feedback_hostA.md"
assert_file_exists "hostA's memory file visible in hostB workspace after pull-only" "$HOSTA_VISIBLE_FILE"
HOSTA_VISIBLE_NOTE="$HOSTB_WS/notes/hostA-note.md"
assert_file_exists "hostA's note visible in hostB workspace after pull-only" "$HOSTA_VISIBLE_NOTE"

# Verify hostB's OWN content still present (merge didn't wipe local)
HOSTB_OWN_FILE="$HOSTB_WS/.claude-sutando/projects/${HOSTB_SLUG}/memory/feedback_hostB.md"
assert_file_exists "hostB's own memory still present after pull-only" "$HOSTB_OWN_FILE"

# Verify hostB's local git head is now a merge commit with 2 parents
cd "$HOSTB_WS"
PARENT_COUNT=$(git cat-file -p HEAD 2>/dev/null | grep -c "^parent " || echo 0)
if [ "$PARENT_COUNT" -ge 2 ]; then
  echo "  OK: hostB HEAD is a merge commit (2+ parents)"; pass=$((pass+1))
else
  echo "  FAIL: hostB HEAD has $PARENT_COUNT parents — expected merge commit"; fail=$((fail+1))
fi
cd "$REPO"

# Confirm the new --allow-unrelated-histories log line fired (visible in stderr)
if echo "$PULL_OUT" | grep -q "unrelated history"; then
  echo "  OK: --pull-only logged 'unrelated history' detection"; pass=$((pass+1))
else
  echo "  FAIL: --pull-only did not log unrelated-history detection; output: $PULL_OUT"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 25: --status pairs WS_ID with local workspace path (wsId↔folder discovery) ===="
# After --init, --status output should include a line that pairs the WS_ID
# with the workspace's absolute path on the local host. This is the
# same-host discovery half of the wsId UX work.

rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"
rm -rf "$FIXTURE_VAULT" && git init -q --bare "$FIXTURE_VAULT"
run_sync --init 2>&1 | head -3 >/dev/null || true

status_out=$(run_sync --status 2>&1 || true)
if echo "$status_out" | grep -E '^WS_ID:' | grep -q "$FIXTURE_WS"; then
  echo "  OK: --status WS_ID line pairs the wsId with its local workspace path"; pass=$((pass+1))
else
  echo "  FAIL: --status WS_ID line missing local workspace path; output: $status_out"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 26: commit messages carry path= for cross-host wsId↔folder discovery ===="
# Both init and push commit messages should include `path=<workspace_path>`
# so a peer host browsing the vault can map host/<host>/<wsId> back to a
# local folder via `git log host/<host>/<wsId>`.

rm -rf "$FIXTURE_WS"
mkdir -p "$FIXTURE_WS"
rm -rf "$FIXTURE_VAULT" && git init -q --bare "$FIXTURE_VAULT"
run_sync --init 2>&1 | head -3 >/dev/null || true

# Verify the init bootstrap commit carries path=
init_msg=$(git --git-dir="$FIXTURE_VAULT" log -1 --pretty=%s "$HOST_BRANCH" 2>/dev/null || echo "")
case "$init_msg" in
  *"path=$FIXTURE_WS"*)
    echo "  OK: init commit message includes path=$FIXTURE_WS"; pass=$((pass+1)) ;;
  *)
    echo "  FAIL: init commit missing path=; msg: $init_msg"; fail=$((fail+1)) ;;
esac

# Trigger a push by writing a tracked-path file, then verify the push commit
mkdir -p "$FIXTURE_WS/notes"
echo "n" > "$FIXTURE_WS/notes/push-msg-test.md"
run_sync --push-only 2>&1 | head -3 >/dev/null || true

push_msg=$(git --git-dir="$FIXTURE_VAULT" log -1 --pretty=%s "$HOST_BRANCH" 2>/dev/null || echo "")
case "$push_msg" in
  *"path=$FIXTURE_WS"*)
    echo "  OK: push commit message includes path=$FIXTURE_WS"; pass=$((pass+1)) ;;
  *)
    echo "  FAIL: push commit missing path=; msg: $push_msg"; fail=$((fail+1)) ;;
esac

# ============================================================================
echo
echo "==== Test 27: pre-wsId flat branch migrates to wsId scheme (no D/F collision) ===="
# Regression for the #1459 follow-up. A leftover flat `host/<host>` branch
# (local + on the vault) is a leaf ref that D/F-conflicts with the nested
# `host/<host>/<wsId>` ref. Pre-fix the pull-side `checkout -B` failed silently
# (error swallowed by `| tee >/dev/null`), stranding HEAD on the flat branch and
# pushing nothing while reporting success. The migration helper must: carry flat
# history into the wsId branch, push the wsId branch to the vault, retire the
# flat branch (local + remote), and leave HEAD on the wsId branch.
T27_VAULT="$TEST_ROOT/vault-27.git"
git init -q --bare "$T27_VAULT"

T27_WS="$TEST_ROOT/ws-27"
T27_REPO="$TEST_ROOT/ws-27-repo"
mkdir -p "$T27_WS/notes" "$T27_REPO/scripts" "$T27_REPO/src"
touch "$T27_REPO/CLAUDE.md"
git init -q "$T27_REPO"
cp "$REPO/scripts/sync-workspace.sh" "$T27_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$T27_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$T27_REPO/src/"
cp "$FIXTURE_REPO/sutando.config.json" "$T27_REPO/"

# Simulate the PRE-wsId on-disk + vault state: a flat `host/mighost` branch.
echo "pre-wsId content" > "$T27_WS/notes/legacy-note.md"
(
  cd "$T27_WS"
  git init -q
  git symbolic-ref HEAD refs/heads/host/mighost     # begin directly on the flat branch
  git remote add origin "$T27_VAULT"
  printf '*\n!notes/\n!notes/**\n' > .gitignore
  git add -A
  git -c user.email=t27@test -c user.name=t27 commit -q -m "legacy flat-branch commit"
  git push -q origin refs/heads/host/mighost:refs/heads/host/mighost
)

# Sanity: vault starts with the flat branch.
if git --git-dir="$T27_VAULT" rev-parse refs/heads/host/mighost >/dev/null 2>&1; then
  echo "  OK: vault starts with flat branch host/mighost"; pass=$((pass+1))
else
  echo "  FAIL: test setup — flat branch not in vault"; fail=$((fail+1))
fi

# Run the NEW sync (default bidirectional). Migration should fire.
T27_OUT=$(env -i HOME="$HOME" PATH="$PATH" \
    SUTANDO_REPO_DIR="$T27_REPO" \
    SUTANDO_WORKSPACE="$T27_WS" \
    SUTANDO_TEST_MODE=1 \
    SUTANDO_HOST_OVERRIDE=mighost \
    SUTANDO_WS_ID_OVERRIDE=t27mig \
    bash "$T27_REPO/scripts/sync-workspace.sh" --vault-url "$T27_VAULT" 2>&1)

if echo "$T27_OUT" | grep -q "migrating local flat branch host/mighost"; then
  echo "  OK: migration message surfaced"; pass=$((pass+1))
else
  echo "  FAIL: no migration message. Output: $T27_OUT"; fail=$((fail+1))
fi

# Vault flat branch retired.
if ! git --git-dir="$T27_VAULT" rev-parse refs/heads/host/mighost >/dev/null 2>&1; then
  echo "  OK: vault flat branch host/mighost retired"; pass=$((pass+1))
else
  echo "  FAIL: vault flat branch host/mighost still present"; fail=$((fail+1))
fi

# Vault wsId branch present, carrying the migrated content.
if git --git-dir="$T27_VAULT" cat-file -e refs/heads/host/mighost/t27mig:notes/legacy-note.md 2>/dev/null; then
  echo "  OK: vault wsId branch host/mighost/t27mig carries migrated content"; pass=$((pass+1))
else
  echo "  FAIL: wsId branch missing or content not migrated"; fail=$((fail+1))
fi

# Local HEAD on the wsId branch; flat branch gone locally.
T27_HEAD=$(cd "$T27_WS" && git symbolic-ref --short HEAD 2>/dev/null || echo "")
assert_eq "local HEAD on wsId branch after migration" "host/mighost/t27mig" "$T27_HEAD"
if ( cd "$T27_WS" && ! git show-ref --quiet refs/heads/host/mighost ); then
  echo "  OK: local flat branch host/mighost deleted"; pass=$((pass+1))
else
  echo "  FAIL: local flat branch host/mighost still present"; fail=$((fail+1))
fi

# ============================================================================
echo
echo "==== Test 28: clean tree re-pushes a commit the remote never received (push-retry gap) ===="
# Regression: a failed/lost initial push left the host branch stale because
# _push_only_impl returned early on a clean tree without checking whether the
# remote actually has HEAD. Authoritative ls-remote check now recovers it.
T28_ROOT="$TEST_ROOT/t28"; mkdir -p "$T28_ROOT"
T28_VAULT="$T28_ROOT/vault.git"; git init -q --bare "$T28_VAULT"
T28_WS="$T28_ROOT/ws"; mkdir -p "$T28_WS/notes"; echo "n" > "$T28_WS/notes/t28.md"
t28_env=(SUTANDO_REPO_DIR="$FIXTURE_REPO" SUTANDO_WORKSPACE="$T28_WS" SUTANDO_TEST_MODE=1 SUTANDO_WS_ID_OVERRIDE=t28ws SUTANDO_HOST_OVERRIDE=t28host)
T28_BR="refs/heads/host/t28host/t28ws"
env "${t28_env[@]}" bash "$SYNC" --vault-url "$T28_VAULT" --init >/dev/null 2>&1
# Simulate a lost / never-completed push: branch gone from the vault, tree clean.
git --git-dir="$T28_VAULT" update-ref -d "$T28_BR" 2>/dev/null
t28_out=$(env "${t28_env[@]}" bash "$SYNC" --vault-url "$T28_VAULT" --push-only 2>&1)
if git --git-dir="$T28_VAULT" show-ref --quiet "$T28_BR"; then
  echo "  OK: clean-tree --push-only restored the unpushed branch to the vault"; pass=$((pass+1))
else
  echo "  FAIL: branch not restored; push-only output: $t28_out"; fail=$((fail+1))
fi
case "$t28_out" in
  *"previously-unpushed"*) echo "  OK: push-only reported the recovery push"; pass=$((pass+1)) ;;
  *) echo "  FAIL: no recovery-push message: $t28_out"; fail=$((fail+1)) ;;
esac
# A subsequent clean tick, remote now up to date, must stay a no-op (not re-push forever).
t28_noop=$(env "${t28_env[@]}" bash "$SYNC" --vault-url "$T28_VAULT" --push-only 2>&1)
case "$t28_noop" in
  *"remote up to date"*) echo "  OK: subsequent clean tick is a no-op (remote up to date)"; pass=$((pass+1)) ;;
  *"previously-unpushed"*) echo "  FAIL: re-pushed when remote already up to date: $t28_noop"; fail=$((fail+1)) ;;
  *) echo "  FAIL: unexpected no-op output: $t28_noop"; fail=$((fail+1)) ;;
esac

# ============================================================================
echo
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
