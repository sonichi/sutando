#!/usr/bin/env bash
# A refused pull resets to the pre-pull commit. Local edits that were never
# committed before the pull are erased by that reset — measured 2026-09-02 on a
# host whose every pull was refused for 18 hours: 34 resets, every uncommitted
# edit under the carrier set gone within 30 minutes of being written.
# The fix commits local edits BEFORE the pull, so the reset cannot reach them.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# The script commits with whatever identity git can derive; a CI runner whose
# user has no GECOS name yields "empty ident name", so pass one through env -i
# exactly as tests/sync-workspace.test.sh exports it for its own runs.
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sync-prepull-test}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sync-prepull-test@invalid}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sync-prepull.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT
pass=0; fail=0
ok()   { echo "  OK: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; fail=$((fail+1)); }
mkhost() {  # name wsid -> sets ${name}_REPO ${name}_WS, runs --init
  local n="$1" wsid="$2" r="$TEST_ROOT/$1-repo" w="$TEST_ROOT/$1-ws"
  mkdir -p "$w/notes" "$r/scripts" "$r/src"; touch "$r/CLAUDE.md"; git init -q "$r"
  cp "$REPO/scripts/sync-workspace.sh" "$REPO/scripts/sutando-config.sh" "$REPO/scripts/python-binary.sh" "$r/scripts/"
  cp "$REPO/src/sutando_config.py" "$r/src/"
  cat > "$r/sutando.config.json" <<'JSON'
{"workspace": {"path": "${REPO_DIR}/workspace"},
 "vault": {"enabled": false, "sync": {"include": ["notes/"], "exclude": []}}}
JSON
  echo "$n note" > "$w/notes/$n-note.md"
  env -i HOME="$HOME" PATH="$PATH" SUTANDO_REPO_DIR="$r" SUTANDO_WORKSPACE="$w" SUTANDO_TEST_MODE=1 \
      GIT_AUTHOR_NAME="$GIT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$GIT_AUTHOR_EMAIL" \
      GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL" \
      SUTANDO_HOST_OVERRIDE="$n" SUTANDO_WS_ID_OVERRIDE="$wsid" \
      bash "$r/scripts/sync-workspace.sh" --vault-url "$VAULT" --init >/dev/null 2>&1
  eval "${n}_REPO=\"$r\"; ${n}_WS=\"$w\""
}
runsync() {  # name wsid args...
  local n="$1" wsid="$2"; shift 2
  local r="$TEST_ROOT/$n-repo" w="$TEST_ROOT/$n-ws"
  env -i HOME="$HOME" PATH="$PATH" SUTANDO_REPO_DIR="$r" SUTANDO_WORKSPACE="$w" SUTANDO_TEST_MODE=1 \
      GIT_AUTHOR_NAME="$GIT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$GIT_AUTHOR_EMAIL" \
      GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL" \
      SUTANDO_HOST_OVERRIDE="$n" SUTANDO_WS_ID_OVERRIDE="$wsid" SUTANDO_FORCE_SYNC="${FORCE:-0}" \
      bash "$r/scripts/sync-workspace.sh" --vault-url "$VAULT" "$@" 2>&1
}
VAULT="$TEST_ROOT/vault.git"; git init -q --bare "$VAULT"
mkhost hostA wsa; mkhost hostB wsb
# host A publishes 60 files; host B pulls them so they are TRACKED on B.
for i in $(seq 1 60); do echo "bulk $i" > "$hostA_WS/notes/bulk-$i.md"; done
runsync hostA wsa --push-only >/dev/null
runsync hostB wsb --pull-only >/dev/null
[ "$(git -C "$hostB_WS" ls-files 'notes/bulk-*' | wc -l | tr -d ' ')" = "60" ] && ok "fixture: hostB tracks the 60 files" || bad "fixture: hostB does not track the 60 files"
# host A deletes all 60 (>SUTANDO_SYNC_MAX_DELETE=50) and publishes.
rm "$hostA_WS"/notes/bulk-*.md
# host A's OWN push tripwire refuses 60 deletions too; the force override is A's
# deliberate choice here, so the fixture reproduces the peer-side shape.
FORCE=1 runsync hostA wsa --push-only >/dev/null 2>&1 || true
git -C "$hostA_WS" ls-files 'notes/bulk-*' | grep -q . && bad "fixture: hostA still tracks bulk files" || ok "fixture: hostA published the 60 deletions"
# host B: an UNCOMMITTED edit to a tracked file, then a pull that will be refused.
echo "edit written before the pull" >> "$hostB_WS/notes/hostB-note.md"
git -C "$hostB_WS" diff --quiet -- notes/hostB-note.md && bad "fixture: edit is not uncommitted" || ok "fixture: the edit is uncommitted at pull time"
OUT="$(runsync hostB wsb --pull-only)"; rc=$?
echo "$OUT" | grep -q "REFUSING pull" && ok "the pull was refused by the mass-delete tripwire (rc=$rc)" || bad "expected a refused pull; got rc=$rc: $(echo "$OUT" | tail -2)"
grep -q "edit written before the pull" "$hostB_WS/notes/hostB-note.md" \
  && ok "THE POINT: the uncommitted edit SURVIVES the refused pull's reset" \
  || bad "THE POINT: the refused pull's reset ERASED the local edit"
git -C "$hostB_WS" log -1 --format=%s -- notes/hostB-note.md | grep -q "Sync hostB" \
  && ok "the edit was committed before the pull (so the reset target contains it)" \
  || bad "the edit is not in a pre-pull commit"
[ "$(git -C "$hostB_WS" ls-files 'notes/bulk-*' | wc -l | tr -d ' ')" = "60" ] && ok "hostB still tracks its 60 files (the refusal held)" || bad "hostB lost tracked files despite the refusal"
# A LOCAL deletion must NOT be swept into the pre-pull commit: the push-side
# tripwire counts staged deletions, and committing them here would bypass it.
rm "$hostB_WS/notes/bulk-1.md"
runsync hostB wsb --pull-only >/dev/null 2>&1 || true
git -C "$hostB_WS" ls-files notes/bulk-1.md | grep -q . && ok "a local deletion is left for the push tripwire (still tracked after the pull)" || bad "the pre-pull commit swallowed a local deletion"
# The safety of committing before the pull rests on generate_exclude running
# BEFORE the add: reordering the two would let an out-of-carrier path ride into
# the vault with every behavioural check above still green. Pin the order.
fn="$(awk '/^_commit_local_pre_pull\(\) \{/,/^\}/' "$REPO/scripts/sync-workspace.sh")"
ex_line="$(printf '%s\n' "$fn" | grep -n 'generate_exclude' | head -1 | cut -d: -f1)"
add_line="$(printf '%s\n' "$fn" | grep -n 'git add ' | head -1 | cut -d: -f1)"
[ -n "$ex_line" ] && [ -n "$add_line" ] && [ "$ex_line" -lt "$add_line" ] \
  && ok "generate_exclude runs BEFORE git add inside _commit_local_pre_pull (order pinned)" \
  || bad "generate_exclude must precede git add in _commit_local_pre_pull (ex=$ex_line add=$add_line)"
printf '\n%s: %d passed, %d failed\n' "sync pre-pull commit" "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "PASS — local edits survive a refused pull"
