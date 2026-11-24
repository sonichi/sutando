#!/bin/bash
# A confirmed core releases the lock of the graceful restart that launched it (GR_RID), and only that one.
# Before: the lock was left to age out (15 min) after every successful restart, so the next restart, a
# runtime switch from the card included, was put off as "another restart is under way".
# Run: bash tests/core-runtime-record-releases-restart-lock.test.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok  $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }

REPO="$T/repo"; WS="$T/ws"; LOCK="$WS/state/locks/graceful-restart.lock"
mkdir -p "$REPO/scripts" "$WS/state/locks"
printf '#!/bin/bash\n[ "$1" = workspace ] && echo "%s"\n' "$WS" > "$REPO/scripts/sutando-config.sh"
# shellcheck source=/dev/null
. "$HERE/src/agent/core-runtime-record.sh"
hold() { rm -rf "$LOCK"; mkdir -p "$LOCK"; printf '%s' "$1" > "$LOCK/rid"; }

hold rid-1; GR_RID=rid-1
record_confirmed_core_runtime claude "" 2>/dev/null
[ ! -e "$LOCK" ] && ok "1 a confirmed core releases the lock of the restart that launched it" || fail "1 the lock is still there"
[ -z "${GR_RID:-}" ] && ok "2 the handle is spent: a later confirmation in this launch releases nothing" || fail "2 GR_RID still set"

hold rid-2; GR_RID=rid-1
release_restart_lock_of_this_launch
[ -d "$LOCK" ] && [ "$(cat "$LOCK/rid")" = rid-2 ] && ok "3 another restart's lock is left alone" || fail "3 a lock that was not ours was removed"

hold rid-3; unset GR_RID
record_confirmed_core_runtime codex "" 2>/dev/null
[ -d "$LOCK" ] && ok "4 a launch that no graceful restart started releases nothing" || fail "4 removed without a handle"

rm -rf "$LOCK"; GR_RID=rid-9
release_restart_lock_of_this_launch && ok "5 no lock at all is not an error" || fail "5 failed with no lock"

printf '#!/bin/bash\nexit 1\n' > "$REPO/scripts/sutando-config.sh"
hold rid-5; GR_RID=rid-5
release_restart_lock_of_this_launch && [ -d "$LOCK" ] && ok "6 no workspace: nothing removed, nothing fails" || fail "6"

echo "core-runtime-record-releases-restart-lock: $PASS ok, $FAIL failed"
[ "$FAIL" = 0 ]
