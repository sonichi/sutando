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
cp "$REPO/src/util_paths.py" "$REPO/src/sutando_config.py" "$SB/src/"
cp "$REPO/src/watcher_identity.py" "$REPO/src/watcher_identity.sh" "$SB/src/"
# The REAL shutdown helper: which gate a scope marks is shared state every
# watcher on the workspace reads, so it must be observable here, not stubbed.
cp "$REPO/src/shutdown.py" "$REPO/src/workspace_default.py" "$SB/src/"
cp -R "$REPO/src/runtime-api" "$SB/src/runtime-api"
cp "$REPO/scripts/python-binary.sh" "$SB/scripts/python-binary.sh"
printf '#!/bin/sh\necho "STUB-STARTUP-REACHED"\n' > "$SB/src/startup.sh"
# The heartbeat handoff is a python call, not a pops_* one: a stub writer logs
# its argv beside the seam's calls so scope can be read for it too.
cat > "$SB/src/core_heartbeat.py" <<'HB'
import os, sys
with open(os.environ["POPS_LOG"], "a") as f:
    f.write("heartbeat " + " ".join(sys.argv[1:]) + "\n")
HB
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
TASKS="$SB/workspace/tasks"

jvec() {   # jvec <argv...> -> the JSON list the seam prints for a pid
  local a s='[' sep=''
  for a; do a="${a//\\/\\\\}"; a="${a//\"/\\\"}"; s="$s$sep\"$a\""; sep=','; done
  printf '%s]' "$s"
}

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

# The intake gates, from the one path owner: the workspace-wide one and each
# instance's own. A scope's mark must land on exactly the gate its scope names.
gate_for() {                    # gate_for <instance-or-empty> -> instance gate path
  "$REPO_PY" "$SB/src/util_paths.py" instance-shutdown-gate "$STATE" ${1:+"$1"}
}
REPO_PY="$(. "$REPO/scripts/python-binary.sh"; resolve_python "$REPO")"
WS_GATE="$("$REPO_PY" "$SB/src/util_paths.py" shutdown-gate "$STATE")"
CORE_GATE="$(gate_for '')"; W1_GATE="$(gate_for worker-1)"; W2_GATE="$(gate_for worker-2)"
[ -n "$WS_GATE" ] && [ -n "$CORE_GATE" ] && [ "$CORE_GATE" != "$WS_GATE" ] \
  && [ "$W1_GATE" != "$CORE_GATE" ] && [ "$W2_GATE" != "$W1_GATE" ]
ck "the shared gate and the three instance gates are four distinct files (harness is sound)" $?
note "ws=$(basename "$WS_GATE")  core=$(basename "$CORE_GATE")  w1=$(basename "$W1_GATE")"
[ "$(dirname "$CORE_GATE")" = "$(dirname "$CORE_SENT")" ]
ck "an instance gate sits beside that instance's watcher record" $?
gates_present() { for g in "$WS_GATE" "$CORE_GATE" "$W1_GATE" "$W2_GATE"; do [ -f "$g" ] && basename "$g"; done | tr '\n' ' ' | sed 's/ $//'; }
clear_gates() { rm -f "$WS_GATE" "$CORE_GATE" "$W1_GATE" "$W2_GATE"; }
arm() {                         # arm: all three watchers live and well-formed
  rm -f "$STATE"/watch-tasks-stream*.pid "$STATE"/watch-tasks-stream*.incarnation
  clear_gates
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
      "POPS_ARGV_$CORE_PID=${CORE_ARGV:-/bin/bash $CODE $TASKS}" \
      "POPS_ARGVV_$CORE_PID=${CORE_ARGVV-$(jvec /bin/bash "$CODE" "$TASKS")}" \
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
refuses "another checkout's code_path" "code_path: .*this checkout runs"

# A record and an argv that AGREE on a foreign checkout are self-consistent and
# still not ours: checkouts may share one workspace, so consistency with the
# record is not identity with this checkout.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
sed -i.bak "s|code_path=.*|code_path=$SB/foreign/src/watch-tasks-stream.sh|" "$CORE_SENT"
CORE_ARGV="/bin/bash $SB/foreign/src/watch-tasks-stream.sh" \
CORE_ARGVV="$(jvec /bin/bash "$SB/foreign/src/watch-tasks-stream.sh")" \
  refuses "a self-consistent FOREIGN checkout's watcher" "code_path: .*this checkout runs"

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
CORE_ARGV="python3 -c pass $CODE" CORE_ARGVV="$(jvec python3 -c pass "$CODE")" \
  refuses "a watcher path passed as DATA" "argv: pid $CORE_PID is not a live watch-tasks-stream"
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
CORE_ARGV="python3 -c pass $CODE" CORE_ARGVV="" \
  refuses "a watcher path passed as DATA, flat string only" "argv: pid $CORE_PID is not a live watch-tasks-stream"

# Two tokens, a shell, and the script name — but a DIFFERENT script is executed.
arm; stamp "$CORE_SENT" "$CORE_PID" "$CORE_KEY" inc-core
CORE_ARGV="/bin/bash $SB/src/other.sh $CODE" CORE_ARGVV="$(jvec /bin/bash "$SB/src/other.sh" "$CODE")" \
  refuses "a shell running some OTHER script" "argv:"

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

# ===================================== (f) argv boundaries come from the kernel
# The notifier launches `/bin/bash <script> <tasks-dir>` and the Monitor `bash
# <script> <tasks-dir>`: an operand follows the script in every real launch, and
# the flattened `ps` text cannot say where the script's path ends. The seam's
# argv LIST can. Without one, the operand-bearing text stays unprovable — the
# adapter never splits it into a guess.
confirms() {                    # confirms <label>
  restart --stop-only; out="$OUT"
  [ "$RC" = 0 ] && [ "$(signalled)" = "$CORE_PID" ]; ck "(f) $1 — confirmed and signalled" $?
  note "signalled: [$(signalled)]  rc=$RC"
  grep -q "^argv_vector $CORE_PID\$" "$LOG"; ck "(f) $1 — the vector was asked of the seam" $?
}
arm; confirms "the notifier form /bin/bash <script> <tasks-dir>"
arm; CORE_ARGV="bash $CODE $TASKS" CORE_ARGVV="$(jvec bash "$CODE" "$TASKS")" \
  confirms "the Monitor form bash <script> <tasks-dir>"

arm; CORE_ARGVV="" refuses "an operand-bearing flat argv with NO vector" "unprovable identity"
arm; CORE_ARGV="/bin/bash $CODE" CORE_ARGVV="" \
  confirms "CONTROL: the no-operand flat argv with NO vector still confirms"

# The SAME flat text as the notifier form; the vector says it is ONE spaced
# script path, so the executed script is not ours and nothing was split.
arm; CORE_ARGVV="$(jvec /bin/bash "$CODE $TASKS")" \
  refuses "a spaced script path that flattens like script + operand" "is not a live watch-tasks-stream"
arm; CORE_ARGVV="not json" \
  refuses "a vector the seam could not hand over intact" "argv vector"

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
# A pattern kill is host-wide whatever scope issues it, so core issues NONE: it
# stops only what its own records name — its watcher and its heartbeat writer.
HOST_KILLS="agent-api.py
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
ALL_KILLS="$(printf '%s\n%s' "$APP_KILLS" "$HOST_KILLS" | sort -u)"

arm; restart --stop-only; out="$OUT"
[ -z "$(killed_patterns)" ]; ck "(e) --scope core issues NO pattern kill at all" $?
note "core pattern kills: [$(killed_patterns | tr '\n' ' ')]"
grep -q '^heartbeat --stop$' "$LOG"; ck "(e) --scope core hands over its own recorded heartbeat writer (--stop)" $?
[ "$(grep -c '^heartbeat ' "$LOG")" = "1" ]; ck "(e) and calls the heartbeat exactly once" $?
grep -q "host-wide components" <<<"$out"; ck "(e) and says which components it deliberately left up" $?
grep -q "bridges, dashboard" <<<"$out"; ck "(e) naming the shared services among them" $?

# The control: a pattern kill re-added OUTSIDE the all-gate shows up in the same
# core-scope log, so the empty list above is the gate, not a blind instrument.
sed 's|^_stop_own_task_watcher "|pops_pattern_kill "dashboard.py"\n&|' \
    "$SB/src/restart.sh" > "$SB/src/restart-perturbed-e.sh"
grep -q '^pops_pattern_kill "dashboard.py"$' "$SB/src/restart-perturbed-e.sh"
ck "(e) CONTROL: the perturbation applied (control is not vacuous)" $?
arm; run bash "$SB/src/restart-perturbed-e.sh" --stop-only
[ "$(killed_patterns)" = "dashboard.py" ]; ck "(e) CONTROL: an ungated pattern kill IS counted under core scope" $?

arm; restart --scope all --stop-only; out="$OUT"
[ "$(killed_patterns)" = "$ALL_KILLS" ]
ck "(e) --scope all kills EXACTLY the host-wide set plus the app-wide set" $?
[ "$(killed_patterns)" != "$ALL_KILLS" ] && diff <(echo "$ALL_KILLS") <(killed_patterns) | sed 's/^/       /'
note "all pattern kills: [$(killed_patterns | tr '\n' ' ')]"
grep -q '^heartbeat --stop$' "$LOG"; ck "(e) --scope all hands over the heartbeat writer too" $?

# The drain wait is on the pattern-killed set: under core nothing in it was
# signalled, so waiting on it would only block on a live (maybe a peer's) service.
arm; restart --scope=all; out="$OUT"
grep -q '^pattern_running ' "$LOG"; ck "(e) --scope all drains on the services it signalled" $?
arm; restart; out="$OUT"
! grep -q '^pattern_running ' "$LOG"; ck "(e) --scope core does not wait on services it never signalled" $?
grep -q "STUB-STARTUP-REACHED" <<<"$out"; ck "(e) and still runs to completion and reaches startup.sh" $?

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

# ================================================= (g) the intake gate a scope marks
# fswatch does not replay, so a gate a watcher does not own costs it the event: the core
# scope marks ONLY its own instance's gate; the workspace-wide gate is reserved for `all`.
arm; restart --stop-only; out="$OUT"
[ "$(gates_present)" = "$(basename "$CORE_GATE")" ]
ck "(g) core --stop-only marks EXACTLY the core's instance gate, and leaves it set" $?
note "gates present: [$(gates_present)]"
[ ! -f "$WS_GATE" ]; ck "(g) ...and never the workspace-wide gate" $?
[ ! -f "$W1_GATE" ] && [ ! -f "$W2_GATE" ]; ck "(g) ...so both workers' intake stays open" $?
grep -q "marking the instance gate (scope core)" <<<"$out"; ck "(g) and says which gate it marked" $?

arm; restart; out="$OUT"
[ -z "$(gates_present)" ]; ck "(g) a plain core restart clears its instance gate before startup" $?
note "gates present after restart: [$(gates_present)]"
grep -q "STUB-STARTUP-REACHED" <<<"$out"; ck "(g) ...and reaches startup.sh" $?

arm; restart --scope all --stop-only; out="$OUT"
[ "$(gates_present)" = "$(basename "$WS_GATE")" ]
ck "(g) --scope all --stop-only marks EXACTLY the workspace-wide gate (every watcher defers)" $?
note "gates present: [$(gates_present)]"
arm; restart --scope all; out="$OUT"
[ -z "$(gates_present)" ]; ck "(g) --scope all clears the workspace-wide gate before startup" $?

# The control: a mark re-pointed at the shared gate under core scope IS visible
# here, so the empty worker gates above are the scoping, not a blind instrument.
sed 's|^  \[ "$SCOPE" = "all" \] && printf .workspace. \|\| printf .instance.$|  printf "workspace"|' \
    "$SB/src/restart.sh" > "$SB/src/restart-perturbed-g.sh"
grep -q '^  printf "workspace"$' "$SB/src/restart-perturbed-g.sh"
ck "(g) CONTROL: the perturbation applied (control is not vacuous)" $?
arm; run bash "$SB/src/restart-perturbed-g.sh" --stop-only
[ "$(gates_present)" = "$(basename "$WS_GATE")" ]
ck "(g) CONTROL: a core scope that marks the shared gate IS caught by this harness" $?

# A worker's gate is keyed like its record: the core's mark can never resolve to it.
arm; restart --stop-only
[ "$(cat "$CORE_GATE" 2>/dev/null | grep -c '"reason"')" = "1" ]
ck "(g) the instance gate carries the reason record shutdown.py writes" $?
clear_gates

# src/stop.sh promises "all services"; the default core scope is not that.
grep -q -- '--scope all' "$REPO/src/stop.sh"; ck "(e) src/stop.sh asks for the scope it promises" $?

restart --scope nonsense; out="$OUT"; ck "(e) an unknown scope is rejected, not defaulted" "$([ "$RC" = 2 ] && echo 0 || echo 1)"
restart --scope worker; out="$OUT"; ck "(e) --scope worker without an id is rejected" "$([ "$RC" = 2 ] && echo 0 || echo 1)"

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
