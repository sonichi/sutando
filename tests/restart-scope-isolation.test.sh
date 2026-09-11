#!/usr/bin/env bash
# Lifecycle isolation between instances: a restart may touch only what its
# declared scope owns, and may signal a watcher only once it has PROVEN that
# watcher is this install's, this instance's and this incarnation's.
#
# Every process operation runs through the injected fake (SUTANDO_PROCESS_OPS),
# so the assertions read a call log rather than a host's process table and no
# real process is signalled at any point.
#
# Run: bash tests/restart-scope-isolation.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAKE="$REPO/tests/fixtures/process-ops-fake.sh"
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }
note() { printf '       %s\n' "$1"; }

SB="$(mktemp -d)"; trap 'rm -rf "$SB"' EXIT

# --- sandbox -----------------------------------------------------------------
# restart.sh resolves REPO from `dirname "$0"/..`, so a copy under $SB/src reads
# only sandbox files. The sentinel resolution is REAL (util_paths + instance_key
# decide the per-instance filename); everything that would touch the host is
# stubbed, absent, or routed to the fake.
mkdir -p "$SB/src" "$SB/scripts" "$SB/bin" "$SB/workspace/state"
cp "$REPO/src/restart.sh" "$REPO/src/watcher_sentinel.sh" "$REPO/src/process-ops.sh" "$SB/src/"
cp "$REPO/src/util_paths.py" "$REPO/src/sutando_config.py" "$REPO/src/watcher_identity.py" "$SB/src/"
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
# Belt and braces only — the isolation guarantee is the fake layer, not these.
for c in pkill pgrep launchctl tmux ngrok lsof; do printf '#!/bin/sh\nexit 1\n' > "$SB/bin/$c"; done
printf '#!/bin/sh\nexit 0\n' > "$SB/bin/sleep"
chmod +x "$SB/src/startup.sh" "$SB/scripts/sutando-config.sh" "$SB/bin/"*

STATE="$SB/workspace/state"
CODE="$SB/src/watch-tasks-stream.sh"

sentinel_for() {                # sentinel_for <instance-or-empty>
  ( . "$SB/src/watcher_sentinel.sh"; sentinel_path_for "$STATE" "$1" )
}
# A full identity record, as the watcher-side writer produces it.
stamp() {                       # stamp <sentinel> <pid> <instance> <incarnation>
  local f="$1"
  printf '%s\ninstance=%s\nincarnation=%s\ncode_path=%s\nversion=test\nstarted_at=%s\nworkspace=%s\n' \
    "$2" "$3" "$4" "$CODE" "$(date +%s)" "$SB/workspace" > "$f"
  printf '%s\n' "$4" > "${f%.pid}.incarnation"
}

CORE_SENT="$(sentinel_for '')"
W1_SENT="$(sentinel_for worker-1)"
W2_SENT="$(sentinel_for worker-2)"
[ -n "$CORE_SENT" ] && [ "$W1_SENT" != "$CORE_SENT" ] && [ "$W2_SENT" != "$W1_SENT" ]
ck "the three instances resolve to three distinct sentinels (harness is sound)" $?
note "core=$(basename "$CORE_SENT")  w1=$(basename "$W1_SENT")  w2=$(basename "$W2_SENT")"

key_of() { local b; b="$(basename "$1")"; b="${b#watch-tasks-stream}"; b="${b%.pid}"; printf '%s' "${b#-}"; }
CORE_KEY="$(key_of "$CORE_SENT")"; W1_KEY="$(key_of "$W1_SENT")"; W2_KEY="$(key_of "$W2_SENT")"

CORE_PID=9101; W1_PID=9102; W2_PID=9103
arm() {                         # arm: all three watchers live and well-formed
  rm -f "$STATE"/watch-tasks-stream*.pid "$STATE"/watch-tasks-stream*.incarnation
  stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
  stamp "$W1_SENT"   "$W1_PID"   "$W1_KEY"   inc-w1
  stamp "$W2_SENT"   "$W2_PID"   "$W2_KEY"   inc-w2
}

LOG="$SB/ops.log"
# Not `$( )`: the rc of the run is an assertion in its own right, and a
# command substitution would report the subshell's.
run() {                         # run <args...> -> $OUT, $RC, call log in $LOG
  : > "$LOG"
  ( cd "$SB" && PATH="$SB/bin:$PATH" env \
      SUTANDO_PROCESS_OPS="$FAKE" POPS_LOG="$LOG" \
      POPS_ALIVE_PIDS="$CORE_PID $W1_PID $W2_PID" \
      "POPS_ARGV_$CORE_PID=${CORE_ARGV:-/bin/bash $CODE}" \
      "POPS_ARGV_$W1_PID=/bin/bash $CODE" \
      "POPS_ARGV_$W2_PID=/bin/bash $CODE" \
      "POPS_ELAPSED_$CORE_PID=${CORE_ELAPSED-10:00}" \
      "POPS_ELAPSED_$W1_PID=10:00" "POPS_ELAPSED_$W2_PID=10:00" \
      POPS_SIGNAL_RC="${POPS_SIGNAL_RC:-0}" \
      POPS_SIGNAL_SURVIVORS="${POPS_SIGNAL_SURVIVORS:-}" \
      SUTANDO_WATCHER_STOP_TICKS="${SUTANDO_WATCHER_STOP_TICKS:-3}" \
      POPS_LAUNCHCTL_PRINT_RC="${POPS_LAUNCHCTL_PRINT_RC:-1}" \
      "$@" > "$SB/out.txt" 2>/dev/null )
  RC=$?
  OUT="$(cat "$SB/out.txt")"
}
restart() { run bash "$SB/src/restart.sh" "$@"; }
signalled() { grep '^signal ' "$LOG" | awk '{print $2}' | sort -u | tr '\n' ' ' | sed 's/ $//'; }
killed_patterns() { grep '^pattern_kill ' "$LOG" | cut -d' ' -f2- | sort -u; }

# ============================================================== (a) core scope
arm
restart --stop-only; out="$OUT"
ck "(a) core --stop-only exits 0 when its watcher is confirmed" "$([ "$RC" = 0 ] && echo 0 || echo 1)"
[ "$(signalled)" = "$CORE_PID" ]; ck "(a) EXACTLY one pid was signalled, and it is the core's" $?
note "signalled: [$(signalled)]  expected: [$CORE_PID]"
[ ! -e "$CORE_SENT" ]; ck "(a) the core's sentinel was released" $?
[ -f "$W1_SENT" ] && [ -f "$W2_SENT" ]; ck "(a) both workers' sentinels are untouched" $?
! grep -q 'watch-tasks' "$LOG"; ck "(a) no pattern match of any kind names a watcher" $?

# The control for (a): a restart.sh that ALSO pattern-killed watchers shows it
# in the same log. Injected at the stop step — --stop-only exits before EOF.
sed 's|^_stop_own_task_watcher "|pops_pattern_kill "watch-tasks"\n&|' \
    "$SB/src/restart.sh" > "$SB/src/restart-perturbed.sh"
grep -q '^pops_pattern_kill "watch-tasks"$' "$SB/src/restart-perturbed.sh"
ck "(a) CONTROL: the perturbation applied (control is not vacuous)" $?
: > "$LOG"
run bash "$SB/src/restart-perturbed.sh" --stop-only
grep -q 'watch-tasks' "$LOG"
ck "(a) CONTROL: a re-added watcher pattern kill IS visible in the call log" $?

# ============================================================ (b) worker scope
arm
restart --scope worker worker-1 --worker-session w1 --worker-socket "$SB/pool.sock"; out="$OUT"
ck "(b) worker scope exits 0" "$([ "$RC" = 0 ] && echo 0 || echo 1)"
[ "$(signalled)" = "$W1_PID" ]; ck "(b) only worker-1's pid was signalled" $?
note "signalled: [$(signalled)]  expected: [$W1_PID]"
[ ! -e "$W1_SENT" ]; ck "(b) worker-1's sentinel was released" $?
[ -f "$CORE_SENT" ] && [ -f "$W2_SENT" ]; ck "(b) the core's and worker-2's sentinels are untouched" $?
[ -z "$(killed_patterns)" ]; ck "(b) worker scope issues NO pattern kill at all" $?
note "pattern kills: [$(killed_patterns | tr '\n' ' ')]"
grep -q "^tmux -S $SB/pool.sock kill-session -t =w1$" "$LOG"
ck "(b) it kills exactly the named session, exact-match (=w1)" $?
[ "$(grep -c '^tmux ' "$LOG")" = "1" ]; ck "(b) and exactly one tmux call" $?
grep -q "STUB-STARTUP-REACHED" <<<"$out"; rc=$?
[ "$rc" -ne 0 ]; ck "(b) worker scope never restarts the core's services" $?

# A worker whose session was not named must still refuse to guess one.
arm
restart --scope worker worker-1; out="$OUT"
[ -z "$(grep '^tmux ' "$LOG")" ]; ck "(b) an unnamed worker session is not guessed" $?
grep -q "no tmux session named" <<<"$out"; ck "(b) and the refusal is said aloud" $?

# ====================================================== (c) ownership refusals
refuses() {                     # refuses <label> <expected-substring>
  restart --stop-only; out="$OUT"
  [ "$RC" = "3" ];                       ck "(c) $1 — distinct rc 3" $?
  [ -z "$(signalled)" ];                 ck "(c) $1 — nothing signalled" $?
  [ -f "$CORE_SENT" ];                   ck "(c) $1 — the sentinel is left in place" $?
  grep -q "OWNERSHIP NOT CONFIRMED" <<<"$out"; ck "(c) $1 — the refusal is reported" $?
  grep -q "$2" <<<"$out";                ck "(c) $1 — the report names the failed check" $?
}

arm; printf '%s\n' "$CORE_PID" > "$CORE_SENT"       # pre-identity, pid only
refuses "a pid-only sentinel" "records a pid only"

arm; stamp "$CORE_SENT" 9999 "$CORE_KEY" inc-core   # dead pid
refuses "a stale pid" "pid 9999 is not alive"

arm; stamp "$CORE_SENT" "$CORE_PID" "not-our-instance" inc-core
refuses "another instance's record" "instance:"

arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
sed -i.bak "s|code_path=.*|code_path=/somewhere/else/watch-tasks-stream.sh|" "$CORE_SENT"
refuses "another checkout's code_path" "code_path: pid $CORE_PID does not run"

arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
printf 'inc-OTHER\n' > "${CORE_SENT%.pid}.incarnation"
refuses "a previous incarnation" "incarnation:"

arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
rm -f "${CORE_SENT%.pid}.incarnation"
refuses "an incarnation with no live marker" "exposes no marker"

arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
sed -i.bak "s|^workspace=.*|workspace=/another/install|" "$CORE_SENT"
refuses "another install's workspace" "workspace:"

# A COMPLETE record is the requirement: an absent claim must never read as
# "matches the empty default", which is exactly what the default core's key is.
for field in instance incarnation code_path workspace; do
  arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
  grep -v "^$field=" "$CORE_SENT" > "$CORE_SENT.tmp" && mv "$CORE_SENT.tmp" "$CORE_SENT"
  refuses "a record with no $field" "$field:"
done

# The misleading DATA argument: kewei's probe. The path is carried as an operand
# of an interpreter that is not running it, and containment alone confirmed it.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
CORE_ARGV="python3 -c pass $CODE" refuses "a watcher path passed as DATA" "argv: pid $CORE_PID is not a live watch-tasks-stream"

# Two tokens, a shell, and the script name — but a DIFFERENT script is executed.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
CORE_ARGV="/bin/bash $SB/src/other.sh $CODE" refuses "a shell running some OTHER script" "argv:"

# The stale/reissued process, through the shared age policy: a pid that started
# AFTER the sentinel was stamped cannot be the process that stamped it.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
touch -t 202601010000 "$CORE_SENT"
CORE_ELAPSED="00:01" refuses "a process younger than its sentinel" "reissued pid"

# ...and an UNMEASURABLE age is not permission either.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
CORE_ELAPSED="" refuses "an unmeasurable process age" "UNMEASURABLE"

# The control for (c): with every field correct the SAME harness confirms and
# signals, so the refusals above are the guard firing, not the fake misbehaving.
arm
restart --stop-only; out="$OUT"
[ "$RC" = 0 ] && [ "$(signalled)" = "$CORE_PID" ]
ck "(c) CONTROL: a fully correct record IS confirmed and signalled" $?

# ============================================ (c2) the stop must actually stop
# The sentinel is the only record of a watcher that is still running. Releasing
# it on a stop that did not happen loses the identity a retry would need.
stop_failed() {                 # stop_failed <label> <expected-substring>
  restart --stop-only; out="$OUT"
  [ "$RC" = "4" ];                       ck "(c2) $1 — distinct rc 4, not success" $?
  [ -f "$CORE_SENT" ];                   ck "(c2) $1 — the sentinel is RETAINED" $?
  grep -q "$2" <<<"$out";                ck "(c2) $1 — the failure is reported" $?
  ! grep -q "task watcher STOPPED" <<<"$out"
  ck "(c2) $1 — and nothing claims the watcher was stopped" $?
}

arm; POPS_SIGNAL_RC=1 stop_failed "a signal that fails" "SIGNAL FAILED for pid $CORE_PID"
arm; POPS_SIGNAL_SURVIVORS="$CORE_PID" stop_failed "a watcher alive after TERM" "STILL ALIVE after TERM"

# The control for (c2): the same harness with a delivering signal releases the
# sentinel and exits 0, so the two retentions above are the guard, not inertia.
arm; restart --stop-only; out="$OUT"
[ "$RC" = 0 ] && [ ! -e "$CORE_SENT" ]
ck "(c2) CONTROL: a delivered signal DOES release the sentinel and exit 0" $?
grep -q '^grace_tick$' "$LOG" || true   # the happy path may need no tick at all

# =========================================== (d) the peer is never named at all
arm
restart --stop-only; out="$OUT"
peer_hits="$(grep -E "(^| )($W1_PID|$W2_PID)( |$)" "$LOG" | grep -v '^alive ' || true)"
[ -z "$peer_hits" ]; ck "(d) no operation in the whole run names a peer's pid" $?
[ -n "$peer_hits" ] && note "$peer_hits"
[ "$(grep -c '^signal ' "$LOG")" = "1" ]; ck "(d) exactly one signal was issued in the entire run" $?

# ================================================== (e) the declared scope sets
CORE_KILLS="agent-api.py
conversation-server
dashboard.py
discord-bridge
ngrok
observability/boot
remote-gateway-bridge
remote-relay-bridge
screen-capture-server
slack-bridge
telegram-bridge"
APP_KILLS="credential-proxy
src/Sutando/Sutando
web-client.ts"

arm; restart --stop-only; out="$OUT"
[ "$(killed_patterns)" = "$CORE_KILLS" ]; ck "(e) --scope core kills EXACTLY its declared set" $?
[ "$(killed_patterns)" != "$CORE_KILLS" ] && diff <(echo "$CORE_KILLS") <(killed_patterns) | sed 's/^/       /'
grep -q "host-wide components" <<<"$out"; ck "(e) and says which components it deliberately left up" $?

arm; restart --scope all --stop-only; out="$OUT"
[ "$(killed_patterns)" = "$(printf '%s\n%s' "$APP_KILLS" "$CORE_KILLS" | sort -u)" ]
ck "(e) --scope all kills EXACTLY the core set plus the app-wide set" $?
[ "$(killed_patterns)" != "$(printf '%s\n%s' "$APP_KILLS" "$CORE_KILLS" | sort -u)" ] && \
  diff <(printf '%s\n%s' "$APP_KILLS" "$CORE_KILLS" | sort -u) <(killed_patterns) | sed 's/^/       /'

# The launchd proxy is app-wide too: it must be reached only under --scope all.
arm; POPS_LAUNCHCTL_PRINT_RC=0 restart --stop-only
[ -z "$(grep '^launchctl ' "$LOG")" ]; ck "(e) --scope core never calls launchctl" $?
arm; POPS_LAUNCHCTL_PRINT_RC=0 restart --scope all --stop-only
grep -q '^launchctl bootout gui/.*credential-proxy$' "$LOG"
ck "(e) --scope all boots out the launchd credential proxy on a stop" $?

arm; restart --scope=all; out="$OUT"
grep -q "STUB-STARTUP-REACHED" <<<"$out"; ck "(e) --scope=all runs to completion and reaches startup.sh" $?
arm; restart; out="$OUT"
grep -q "Sutando.app not relaunched" <<<"$out"
ck "(e) core scope does not relaunch an app it never stopped" $?

# --rebuild-app replaces the app binary, so it must reach the app lifecycle even
# though the caller named no scope: under core it built over a live app.
arm; restart --rebuild-app --stop-only; out="$OUT"
grep -q "implies --scope all" <<<"$out"; ck "(e) --rebuild-app says it widened the scope" $?
grep -q '^pattern_kill src/Sutando/Sutando$' "$LOG"
ck "(e) --rebuild-app DOES stop the app it is about to replace" $?

# src/stop.sh promises "all services"; the default core scope is not that.
grep -q -- '--scope all' "$REPO/src/stop.sh"; ck "(e) src/stop.sh asks for the scope it promises" $?

restart --scope nonsense; out="$OUT"; ck "(e) an unknown scope is rejected, not defaulted" "$([ "$RC" = 2 ] && echo 0 || echo 1)"
restart --scope worker; out="$OUT"; ck "(e) --scope worker without an id is rejected" "$([ "$RC" = 2 ] && echo 0 || echo 1)"

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
