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

# Order: the selection and the confirmed-core record are both published while the lock is still held,
# so a switch waiting on the lock reads this launch's result, not the state before it.
SEEN="$T/seen"; : > "$SEEN"
cat > "$REPO/scripts/sutando-config.sh" <<CFG
#!/bin/bash
case "\$1" in
  workspace) echo "$WS" ;;
  desktop-runtime-select) [ -d "$LOCK" ] && echo "selection:held" >> "$SEEN" || echo "selection:released" >> "$SEEN"; echo updated ;;
esac
CFG
cat > "$T/py" <<PYSTUB
#!/bin/bash
[ -d "$LOCK" ] && echo "record:held" >> "$SEEN" || echo "record:released" >> "$SEEN"
exit \${PY_RC:-0}
PYSTUB
chmod +x "$T/py"
tmux() { echo 1700000000; }
TMUX_SOCKET="$T/sock"; SESSION=sutando-core
hold rid-7; GR_RID=rid-7; SUTANDO_DESKTOP_RUNTIME_SELECT=claude
record_confirmed_core_runtime claude "$T/py" 2>/dev/null
[ "$(tr '\n' ' ' < "$SEEN")" = "selection:held record:held " ] && [ ! -e "$LOCK" ] \
  && ok "7 the selection and the record are published before the lock is released" || fail "7 order was: $(tr '\n' ' ' < "$SEEN")"
RESULT="$WS/state/graceful-restart-result.json"
field() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$RESULT" "$1" 2>/dev/null; }
[ "$(field rid) $(field runtime) $(field published)" = "rid-7 claude True" ] \
  && ok "7b its result is left under its own id: the runtime, and that the record was published" || fail "7b $(cat "$RESULT" 2>/dev/null)"

# A record that could not be written still ends the restart: the lock is released, after the attempt.
: > "$SEEN"; hold rid-8; GR_RID=rid-8
PY_RC=1 record_confirmed_core_runtime claude "$T/py" 2>/dev/null
[ "$(cat "$SEEN")" = "record:held" ] && [ ! -e "$LOCK" ] && ok "8 a failed record is attempted first, then the lock goes" || fail "8 $(cat "$SEEN")"
[ "$(field rid) $(field published)" = "rid-8 False" ] \
  && ok "8b and the result says the publication failed" || fail "8b $(cat "$RESULT" 2>/dev/null)"

# No session creation time: nothing is recorded, and the lock still goes last.
tmux() { echo ""; }
: > "$SEEN"; hold rid-9; GR_RID=rid-9
record_confirmed_core_runtime claude "$T/py" 2>/dev/null
[ ! -s "$SEEN" ] && [ ! -e "$LOCK" ] && ok "9 an early return still releases the lock" || fail "9"
[ "$(field rid) $(field published)" = "rid-9 False" ] && ok "9b with a result that claims no publication" || fail "9b $(cat "$RESULT" 2>/dev/null)"

# A lock that is not ours: no result is written for it, and an id that is not plain writes none either.
rm -f "$RESULT"; hold rid-other; GR_RID=rid-10
record_confirmed_core_runtime claude "$T/py" 2>/dev/null
[ ! -e "$RESULT" ] && [ -d "$LOCK" ] && ok "10 no result for a lock another restart holds" || fail "10"
hold 'r"id'; GR_RID='r"id'
release_restart_lock_of_this_launch claude
[ ! -e "$RESULT" ] && [ ! -e "$LOCK" ] && ok "11 an id that is not plain is released without a result" || fail "11 $(cat "$RESULT" 2>/dev/null)"

printf '#!/bin/bash\nexit 1\n' > "$REPO/scripts/sutando-config.sh"
hold rid-5; GR_RID=rid-5
release_restart_lock_of_this_launch && [ -d "$LOCK" ] && ok "6 no workspace: nothing removed, nothing fails" || fail "6"

echo "core-runtime-record-releases-restart-lock: $PASS ok, $FAIL failed"
[ "$FAIL" = 0 ]
