#!/usr/bin/env bash
# _commit_local_pre_pull() ran generate_exclude then `git add --ignore-removal
# .` and committed, but never ran the carrier-enforcement UNTRACK walk that
# _push_only_impl runs before ITS git add. A tracked file whose exclude
# coverage widens is still tracked at pre-pull-commit time, so `git add`
# re-stages its latest (now-denied) content and the pre-pull commit captures
# it -- a LATER push's own enforcement then untracks it going forward, but
# the private content survives forever in that earlier commit's tree, pushed
# to the vault as an ancestor of HEAD (#4309 review round 7, keweichen).
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sync-untrack-test}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sync-untrack-test@invalid}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sync-untrack.XXXXXX")"
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
      SUTANDO_HOST_OVERRIDE="$n" SUTANDO_WS_ID_OVERRIDE="$wsid" \
      bash "$r/scripts/sync-workspace.sh" --vault-url "$VAULT" "$@" 2>&1
}
VAULT="$TEST_ROOT/vault.git"; git init -q --bare "$VAULT"
mkhost hostU wsu

# The file is tracked normally, before any exclude rule covers it.
echo "public v1" > "$hostU_WS/notes/private-log.jsonl"
PUSH_OUT="$(runsync hostU wsu --push-only)"; PUSH_RC=$?
if ! git -C "$hostU_WS" ls-files --error-unmatch notes/private-log.jsonl >/dev/null 2>&1; then
  bad "fixture: notes/private-log.jsonl never got tracked -- fixture bug, push-only rc=$PUSH_RC: ${PUSH_OUT:0:300}"
  printf '\n%s: %d passed, %d failed\n' "sync pre-pull untrack-before-commit" "$pass" "$fail"
  echo "FAIL — fixture never reached the state this test exists to exercise"
  exit 1
fi
ok "fixture: notes/private-log.jsonl is tracked"

# Exclude coverage now widens to deny it (a real config edit, e.g. a
# transcript path getting added to vault.sync.exclude) -- then it is
# modified BEFORE the next pull, exactly as a live session would edit it.
python3 - "$hostU_REPO/sutando.config.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["vault"]["sync"]["exclude"] = ["notes/private-log.jsonl"]
json.dump(d, open(p, "w"))
PY
echo "PRIVATE-CONTENT-MARKER" >> "$hostU_WS/notes/private-log.jsonl"

# --pull-only triggers _commit_local_pre_pull; --force-gitignore lets the
# newly-added exclude rule land immediately (an operator-widened rule set,
# not an operator-authored conflict -- this refusal path is orthogonal to
# the bug under test).
runsync hostU wsu --pull-only --force-gitignore >/dev/null 2>&1 || true

# THE POINT: the private content must not be reachable in ANY commit's tree
# in this repo's history -- not just absent from the current HEAD.
if git -C "$hostU_WS" log --all -p -- notes/private-log.jsonl 2>/dev/null | grep -q "PRIVATE-CONTENT-MARKER"; then
  bad "THE POINT: PRIVATE-CONTENT-MARKER reached a commit in this repo's history"
else
  ok "THE POINT: the newly-denied edit never entered any commit"
fi

# Push it (mirrors the vault round trip) and confirm the bare vault's history
# is equally clean -- the leak keweichen reported is specifically that a
# LATER push's own untrack does not retroactively clean an EARLIER commit.
runsync hostU wsu --push-only >/dev/null 2>&1 || true
if git --git-dir="$VAULT" log --all -p -- notes/private-log.jsonl 2>/dev/null | grep -q "PRIVATE-CONTENT-MARKER"; then
  bad "the pushed vault history contains PRIVATE-CONTENT-MARKER"
else
  ok "the pushed vault history never received PRIVATE-CONTENT-MARKER"
fi

# --- Structural: the untrack step must run BEFORE `git add` inside
# _commit_local_pre_pull, mirroring _push_only_impl's own ordering.
fn="$(awk '/^_commit_local_pre_pull\(\) \{/,/^\}/' "$REPO/scripts/sync-workspace.sh")"
enforce_line="$(printf '%s\n' "$fn" | grep -n '_enforce_carrier_set_pre' | head -1 | cut -d: -f1)"
add_line="$(printf '%s\n' "$fn" | grep -n 'git add ' | head -1 | cut -d: -f1)"
[ -n "$enforce_line" ] && [ -n "$add_line" ] && [ "$enforce_line" -lt "$add_line" ] \
  && ok "_enforce_carrier_set_pre runs BEFORE git add inside _commit_local_pre_pull" \
  || bad "_enforce_carrier_set_pre must precede git add in _commit_local_pre_pull (enforce=$enforce_line add=$add_line)"

printf '\n%s: %d passed, %d failed\n' "sync pre-pull untrack-before-commit" "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "PASS — pre-pull commit cannot capture a newly-denied tracked path"
