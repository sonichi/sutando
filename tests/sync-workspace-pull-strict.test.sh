#!/usr/bin/env bash
# The default tick returns the PUSH's rc: a refused pull exits 0, so a caller
# reading that zero as "my view of peer state is current" reads a lie (#3717).
# --pull-strict runs the same two legs and lets the pull leg say no (exit 3).
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sync-strict-test}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sync-strict-test@invalid}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sync-strict.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT
pass=0; fail=0
ok()   { echo "  OK: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; fail=$((fail+1)); }
mkhost() {  # name wsid -> builds an isolated repo+workspace pair and runs --init
  local n="$1" wsid="$2" r="$TEST_ROOT/$1-repo" w="$TEST_ROOT/$1-ws"
  mkdir -p "$w/notes" "$r/scripts" "$r/src"; touch "$r/CLAUDE.md"; git init -q "$r"
  cp "$REPO/scripts/sync-workspace.sh" "$REPO/scripts/sutando-config.sh" "$REPO/scripts/python-binary.sh" "$r/scripts/"
  cp "$REPO/src/sutando_config.py" "$r/src/"
  cat > "$r/sutando.config.json" <<'JSON'
{"workspace": {"path": "${REPO_DIR}/workspace"},
 "vault": {"enabled": false, "sync": {"include": ["notes/"], "exclude": []}}}
JSON
  echo "$n note" > "$w/notes/$n-note.md"
  runsync "$n" "$wsid" --init >/dev/null 2>&1
}
runsync() {  # name wsid args... -> runs sync-workspace in that host's sandbox
  local n="$1" wsid="$2"; shift 2
  local r="$TEST_ROOT/$n-repo" w="$TEST_ROOT/$n-ws"
  env -i HOME="$HOME" PATH="$PATH" SUTANDO_REPO_DIR="$r" SUTANDO_WORKSPACE="$w" SUTANDO_TEST_MODE=1 \
      GIT_AUTHOR_NAME="$GIT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$GIT_AUTHOR_EMAIL" \
      GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL" \
      SUTANDO_HOST_OVERRIDE="$n" SUTANDO_WS_ID_OVERRIDE="$wsid" SUTANDO_FORCE_SYNC="${FORCE:-0}" \
      bash "$r/scripts/sync-workspace.sh" ${VAULT:+--vault-url "$VAULT"} "$@" 2>&1
}

VAULT="$TEST_ROOT/vault.git"; git init -q --bare "$VAULT"
mkhost hostA wsa; mkhost hostB wsb

echo "-- healthy fleet: both modes succeed --"
OUT="$(runsync hostB wsb)"; rc=$?
[ "$rc" = "0" ] && ok "default tick on a healthy fleet exits 0" || bad "default tick exited $rc: $(echo "$OUT" | tail -2)"
OUT="$(runsync hostB wsb --pull-strict)"; rc=$?
[ "$rc" = "0" ] && ok "--pull-strict on a healthy fleet exits 0 (no false alarm)" || bad "--pull-strict exited $rc: $(echo "$OUT" | tail -2)"

echo "-- a pull the mass-deletion tripwire refuses --"
# host A publishes 60 files, host B tracks them, then host A deletes all 60:
# host B's next pull is refused by the production tripwire (>50 deletions).
for i in $(seq 1 60); do echo "bulk $i" > "$TEST_ROOT/hostA-ws/notes/bulk-$i.md"; done
runsync hostA wsa --push-only >/dev/null
runsync hostB wsb --pull-only >/dev/null
[ "$(git -C "$TEST_ROOT/hostB-ws" ls-files 'notes/bulk-*' | wc -l | tr -d ' ')" = "60" ] \
  && ok "fixture: hostB tracks the 60 files" || bad "fixture: hostB does not track the 60 files"
rm "$TEST_ROOT/hostA-ws"/notes/bulk-*.md
FORCE=1 runsync hostA wsa --push-only >/dev/null 2>&1 || true

# Each mode gets its own pre-pull state: the refusal must be reproduced twice.
OUT_DEFAULT="$(runsync hostB wsb)"; rc_default=$?
echo "$OUT_DEFAULT" | grep -q "REFUSING pull" \
  && ok "fixture: the default tick's pull leg WAS refused" || bad "fixture: no refusal in the default tick"
[ "$rc_default" = "0" ] \
  && ok "default mode is unchanged: refused pull, successful push, exit 0" \
  || bad "default mode changed behaviour: exit $rc_default (expected 0)"

# A file only the strict run can publish, so "did the push leg run?" below has
# an answer that is observable in the vault rather than inferred from a log line.
echo "written before the strict tick" > "$TEST_ROOT/hostB-ws/notes/strict-marker.md"
OUT_STRICT="$(runsync hostB wsb --pull-strict)"; rc_strict=$?
echo "$OUT_STRICT" | grep -q "REFUSING pull" \
  && ok "fixture: the strict tick's pull leg WAS refused too" || bad "fixture: no refusal in the strict tick"
[ "$rc_strict" = "3" ] \
  && ok "THE POINT: --pull-strict exits 3 on the same refusal the default reports as 0" \
  || bad "THE POINT: --pull-strict exited $rc_strict, not 3"
echo "$OUT_STRICT" | grep -q "STRICT FAILURE — pull leg" \
  && ok "--pull-strict names the failing leg on stderr" || bad "--pull-strict does not name the pull leg"
# The strict run must still have PUSHED: the gate's caller needs its own records
# published, so strict must not degrade into --pull-only.
git --git-dir="$VAULT" show "host/hostB/wsb:notes/strict-marker.md" 2>/dev/null | grep -q "written before the strict tick" \
  && ok "--pull-strict still PUSHED (the marker reached the vault): it is the full tick, not --pull-only" \
  || bad "--pull-strict skipped the push leg (no marker in the vault)"

echo "-- sync disabled: a pull that never ran is not a fresh view --"
VAULT="" OUT="$(VAULT="" runsync hostB wsb)"; rc=$?
[ "$rc" = "0" ] && ok "default tick skips cleanly with no vault URL (cron stays quiet)" || bad "default tick exited $rc with no vault URL"
OUT="$(VAULT="" runsync hostB wsb --pull-strict)"; rc=$?
[ "$rc" = "3" ] && ok "--pull-strict exits 3 when sync is disabled (no pull ran)" || bad "--pull-strict exited $rc with no vault URL"
VAULT="$TEST_ROOT/vault.git"

echo "-- the seam itself: no caller may read the default's rc as freshness --"
# Pin the contract in the source, so a future edit that re-swallows the pull rc
# fails here rather than silently re-opening the bypass.
grep -q '_pull_only_impl || _pull_rc=\$?' "$REPO/scripts/sync-workspace.sh" \
  && ok "the shared tick captures the pull leg's rc instead of discarding it" \
  || bad "the shared tick no longer captures the pull rc"
grep -q '_pull_only_impl || true' "$REPO/scripts/sync-workspace.sh" \
  && bad "a '_pull_only_impl || true' call site is back" \
  || ok "no call site discards the pull's rc with '|| true'"

echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
