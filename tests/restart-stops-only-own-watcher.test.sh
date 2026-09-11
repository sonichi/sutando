#!/usr/bin/env bash
# restart.sh must stop THIS core's task watcher and no other.
#
# Measured 2026-09-10 on a pool host: `pkill -f "watch-tasks"` matched every
# watcher on the machine, so a core restart through the desktop app killed the
# pool workers' drains and their rooms went silent. Ownership is named by the
# sentinel (state/watch-tasks-stream*.pid), never by an argv pattern.
#
# The kill is a real one: two decoy processes whose argv contains
# `watch-tasks-stream` run in the sandbox, only ONE of them is named by a
# sentinel, and the test asserts which of the two is still alive afterwards.
# No process-ops fake here — this file deliberately exercises the REAL signal
# path end to end, which the call-log tests in restart-scope-isolation cannot.
#
# Run: bash tests/restart-stops-only-own-watcher.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
DECOYS=""
cleanup() {
  for p in $DECOYS; do kill "$p" 2>/dev/null; done
  rm -rf "$SB"
}
trap cleanup EXIT

# --- sandbox -----------------------------------------------------------------
# restart.sh resolves REPO from `dirname "$0"/..`, so a copy under $SB/src reads
# only sandbox files. Everything the watcher-sentinel resolution needs is real;
# everything that would touch the host (startup, shutdown, heartbeat, pkill,
# pgrep, launchctl, the workspace lookup) is absent or stubbed.
mkdir -p "$SB/src" "$SB/scripts" "$SB/bin" "$SB/workspace/state" "$SB/own" "$SB/peer"
cp "$REPO/src/restart.sh" "$SB/src/restart.sh"
cp "$REPO/src/watcher_sentinel.sh" "$REPO/src/process-ops.sh" "$SB/src/"
cp "$REPO/src/util_paths.py" "$REPO/src/sutando_config.py" "$SB/src/"
cp -R "$REPO/src/runtime-api" "$SB/src/runtime-api"
cp "$REPO/scripts/python-binary.sh" "$SB/scripts/python-binary.sh"

printf '#!/bin/sh\necho "STUB-STARTUP-REACHED"\n' > "$SB/src/startup.sh"
cat > "$SB/scripts/sutando-config.sh" <<CFG
#!/bin/sh
case "\$1" in
  workspace) printf '%s' "$SB/workspace" ;;
  *) exit 1 ;;
esac
CFG
cat > "$SB/bin/pkill" <<PK
#!/bin/sh
echo "STUB-PKILL \$*"
echo "\$*" >> "$SB/pkill.log"
exit 0
PK
for c in pgrep launchctl ngrok; do printf '#!/bin/sh\nexit 1\n' > "$SB/bin/$c"; done
printf '#!/bin/sh\nexit 0\n' > "$SB/bin/sleep"
: > "$SB/pkill.log"

# A decoy whose argv carries the watcher's own name. /bin/sleep by absolute path
# so the stubbed `sleep` on PATH cannot turn this into a busy loop.
for d in own peer; do
  cat > "$SB/$d/watch-tasks-stream.sh" <<'DEC'
#!/bin/bash
while :; do /bin/sleep 0.2; done
DEC
done
chmod +x "$SB/src/startup.sh" "$SB/scripts/sutando-config.sh" "$SB/bin/"* "$SB"/*/watch-tasks-stream.sh

bash "$SB/own/watch-tasks-stream.sh"  & OWN_PID=$!
bash "$SB/peer/watch-tasks-stream.sh" & PEER_PID=$!
DECOYS="$OWN_PID $PEER_PID"
disown "$OWN_PID" "$PEER_PID" 2>/dev/null   # job-control "Terminated" notices are not output

# The sentinel path comes from the sandbox's own resolver, so the test cannot
# disagree with restart.sh about which file names this instance.
SENTINEL="$( . "$SB/src/watcher_sentinel.sh"; sentinel_path_for "$SB/workspace/state" )"
[ -n "$SENTINEL" ]; ck "sentinel path resolved in the sandbox (harness is sound)" $?

# The identity record the watcher-side writer produces. A bare pid proves
# nothing about WHICH watcher wears it, so restart.sh refuses to signal one
# (checked at the end of this file).
stamp() {                       # stamp <pid> <code_path> [incarnation]
  printf '%s\ninstance=\nincarnation=%s\ncode_path=%s\nversion=test\nworkspace=%s\n' \
    "$1" "${3:-inc1}" "$2" "$SB/workspace" > "$SENTINEL"
  printf '%s\n' "${3:-inc1}" > "${SENTINEL%.pid}.incarnation"
}
stamp "$OWN_PID" "$SB/own/watch-tasks-stream.sh"

alive() { kill -0 "$1" 2>/dev/null; }
alive "$OWN_PID" && alive "$PEER_PID"; ck "both decoy watchers are running before the restart" $?

out="$( cd "$SB" && PATH="$SB/bin:$PATH" bash "$SB/src/restart.sh" 2>/dev/null )"

grep -q "STUB-STARTUP-REACHED" <<<"$out"; ck "restart.sh runs to completion under stubs" $?

# --- the fix -----------------------------------------------------------------
# Poll: the decoy defers SIGTERM until its current /bin/sleep returns.
for _ in $(seq 1 40); do alive "$OWN_PID" || break; /bin/sleep 0.1; done
! alive "$OWN_PID"; ck "the watcher named by THIS core's sentinel was stopped" $?
alive "$PEER_PID"; ck "a peer watcher with no sentinel of ours SURVIVED" $?
[ ! -e "$SENTINEL" ]; ck "our sentinel was removed" $?

# --- the control -------------------------------------------------------------
# The regression was a pattern kill. Its stub line must not appear at all.
! grep -q "STUB-PKILL .*watch-tasks" <<<"$out"
ck "no pkill -f watch-tasks remains (the pool-host kill is gone)" $?
# The peer above survived a STUBBED pkill, so also ask what the real one WOULD
# have matched: every pattern restart.sh issued, against the peer's own argv.
peer_argv="$(ps -p "$PEER_PID" -o args= 2>/dev/null)"
would=0
while read -r flag pat; do
  [ "$flag" = "-f" ] && [ -n "$pat" ] || continue
  case "$peer_argv" in *"$pat"*) would=1 ;; esac
done < "$SB/pkill.log"
[ "$would" -eq 0 ]; ck "no pattern kill it issues would have matched the peer watcher" $?
grep -q "watcher stop: signalling this core's watcher (pid $OWN_PID)" <<<"$out"
ck "the run names the pid it targeted" $?

# The #4163 warning still follows the stop it describes.
grep -q "task watcher STOPPED" <<<"$out"; ck "the watcher-stopped warning is still emitted" $?
stop_n=$(grep -n "watcher stop: signalling this core's watcher" <<<"$out" | head -1 | cut -d: -f1)
warn_n=$(grep -n "task watcher STOPPED"              <<<"$out" | head -1 | cut -d: -f1)
[ -n "$stop_n" ] && [ -n "$warn_n" ] && [ "$warn_n" -gt "$stop_n" ]
ck "the warning follows the stop, not precedes it" $?

# --- reissued pid ------------------------------------------------------------
# A sentinel naming a live process that is NOT a watcher must not authorise a
# kill: that number belongs to whoever inherited it.
/bin/sleep 30 & INNOCENT=$!
DECOYS="$DECOYS $INNOCENT"
disown "$INNOCENT" 2>/dev/null
stamp "$INNOCENT" "$SB/own/watch-tasks-stream.sh"
out2="$( cd "$SB" && PATH="$SB/bin:$PATH" bash "$SB/src/restart.sh" 2>/dev/null )"
alive "$INNOCENT"; ck "a sentinel pid whose argv is not a watcher is NOT killed" $?
grep -q "argv: pid $INNOCENT is not a live watch-tasks-stream" <<<"$out2"; ck "and the refusal is said aloud" $?

# --- the peer watcher, named by a record that is not ours --------------------
# The peer is a genuine, live watcher running this very checkout — every check
# an argv scan can make passes. Only its recorded identity says it is another
# instance's, and that alone must be enough to refuse.
printf '%s\ninstance=other-worker\nincarnation=inc1\ncode_path=%s\nversion=test\nworkspace=%s\n' \
  "$PEER_PID" "$SB/peer/watch-tasks-stream.sh" "$SB/workspace" > "$SENTINEL"
printf 'inc1\n' > "${SENTINEL%.pid}.incarnation"
out3="$( cd "$SB" && PATH="$SB/bin:$PATH" bash "$SB/src/restart.sh" 2>/dev/null )"
alive "$PEER_PID"; ck "a live peer watcher recorded under another instance SURVIVES" $?
grep -q "instance: .* says \"other-worker\"" <<<"$out3"; ck "and the report names the check that refused" $?

# --- a pre-identity sentinel ------------------------------------------------
# Before the watcher writes a record there is nothing to check the pid against,
# and `kill -0` answering is not ownership. Refuse rather than guess.
printf '%s\n' "$PEER_PID" > "$SENTINEL"
out4="$( cd "$SB" && PATH="$SB/bin:$PATH" bash "$SB/src/restart.sh" 2>/dev/null )"; rc4=$?
alive "$PEER_PID"; ck "a pid-only sentinel does NOT authorise a signal" $?
grep -q "records a pid only" <<<"$out4"; ck "and says the record is what is missing" $?
[ -f "$SENTINEL" ]; ck "an unconfirmed sentinel is left in place, not deleted" $?

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
