#!/usr/bin/env bash
# A pool worker's session is switched and recorded per worker only while it RUNS in a Claude folder of its own.
# tmux, pgrep and ps are PATH shims over temp folders; the fake CLI saves /model into the folder the session runs in.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; export TMUX_FAKE_UNWRAP="$HERE/tests/lib/tmux-fake-unwrap.sh"
T="$(cd "$(mktemp -d)" && pwd -P)"; trap 'rm -rf "$T"' EXIT
A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; B=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; C=cccccccccccccccccccccccccccccccc
CORE="$T/claude-core"; STATE="$T/ws/state"
mkdir -p "$T/bin" "$T/tmp" "$T/proc" "$T/panes" "$CORE/skills" "$STATE/workers/$A" "$STATE/workers/$B"
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
sock=""; prev=""; for a in "$@"; do [ "$prev" = -S ] && sock="$a"; prev="$a"; done; sb="$(basename "$sock")"; panes="$FAKE_ROOT/panes/$sb"
# A pane is "<pane id> <pid>" in its session's file; a target is a session name or a pane id.
pid_of() { case "$1" in %*) cat "$panes"/* 2>/dev/null | awk -v id="$1" '$1==id{print $2}';; *) awk '{print $2}' "$panes/$1" 2>/dev/null;; esac; }
swap() { [ "${FAKE_SWAP%%:*}" = "$1" ] && { r="${FAKE_SWAP#*:}"; f="$panes/${r%%:*}"; echo "$(awk '{print $1}' "$f") ${r#*:}" > "$f"; }; }
# A send bound to a pane pid is withheld, as tmux withholds it, when the pane now has another process.
want="$(printf '%s' "$*" | sed -n 's/.*#{==:#{pane_pid},\([0-9]*\)}.*/\1/p')"
if [ -n "$want" ]; then
  to="$(printf '%s' "$*" | sed -n "s/.*copy-mode -q -t '\([^']*\)' ; .*/\1/p")"
  [ "$(pid_of "$to")" = "$want" ] || { echo "WITHHELD $to" >> "$TMUX_LOG"; exit 1; }
fi
. "$TMUX_FAKE_UNWRAP"
printf '%s\n' "$*" >> "$TMUX_LOG"
target=""; prev=""; for a in "$@"; do [ "$prev" = -t ] && target="${a#=}"; prev="$a"; done
case " $* " in
  *" list-windows "*) echo 0; exit 0;;
  *" has-session "*) [ -f "$panes/$target" ]; exit $?;;
  *" list-panes "*) [ -f "$panes/$target" ] || exit 1; cat "$panes/$target"; swap "panes$(grep -c ' list-panes ' "$TMUX_LOG")"; exit 0;;
  *" send-keys "*" Enter ") swap enter;;
esac
case " $* " in *" capture-pane "*)
  bound=""; case " $* " in *" display-message -p "*" ; capture-pane "*) bound="$(pid_of "$target")"; [ -n "$bound" ] || exit 1; echo "$bound";; esac
  seen=""; prev=""; for a in "$@"; do [ "$a" = capture-pane ] && seen=1; [ -n "$seen" ] && [ "$prev" = -t ] && { target="$a"; break; }; prev="$a"; done
  # One acceptance line per /model sent, on the pane it was sent to; the pick is saved in the folder that session runs in.
  acc=""; sent="$(grep -- "-l /model" "$TMUX_LOG" | tail -1)"
  if [ -n "$sent" ] && [ -z "${FAKE_NO_ACCEPT:-}" ]; then
    model="${sent##*-l /model }"; session="$(printf '%s' "$sent" | sed 's/.*send-keys -t \([^ ]*\) .*/\1/')"
    case "$model" in opus) name="Opus 5";; haiku) name="Haiku 4.5";; sonnet) name="Sonnet 5";; *) name="$model";; esac
    if [ "$target" = "$session" ] || [ "$target" = "$(awk '{print $1}' "$panes/$session" 2>/dev/null)" ]; then
      acc="  ⎿  Set model to $name and saved as your default\n"
      folder="$(cat "$FAKE_ROOT/proc/$session.folder" 2>/dev/null)"
      [ -n "$folder" ] && printf '{"model":"%s"}\n' "$model" > "$folder/settings.json"
    fi
  fi
  # What some other pane shows once a /model was sent: text planted for this exact target on this socket.
  [ -n "$sent" ] && acc="$acc$(cat "$FAKE_ROOT/screen/$sb/$target" 2>/dev/null)"
  printf '%b' "$acc────\n❯ \n────\n"
  [ -n "$bound" ] && echo "$bound";; esac
exit 0
SH
cat > "$T/bin/pgrep" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = -P ] && cat "$FAKE_ROOT/proc/$2.kids" 2>/dev/null && exit 0
exit 1
SH
cat > "$T/bin/ps" <<'SH'
#!/usr/bin/env bash
pid=""; prev=""; for a in "$@"; do [ "$prev" = -p ] && pid="$a"; prev="$a"; done
[ -f "$FAKE_ROOT/proc/$pid.args" ] || exec /bin/ps "$@"
case " $* " in
  *" eww "*) printf '%s %s\n' "$(cat "$FAKE_ROOT/proc/$pid.args")" "$(cat "$FAKE_ROOT/proc/$pid.env" 2>/dev/null)";;
  *) cat "$FAKE_ROOT/proc/$pid.args";;
esac
SH
chmod +x "$T/bin/tmux" "$T/bin/pgrep" "$T/bin/ps"
# A fake process: its argv and the environment it was started with. pane puts one in a session's pane on a socket.
proc() { # proc PID SESSION FOLDER [ENV-OVERRIDE]
  echo "claude --name $2 --dangerously-skip-permissions" > "$T/proc/$1.args"
  printf '%s' "${4-HOME=$T SUTANDO_TMUX_SESSION=$2 CLAUDE_CONFIG_DIR=$3 PATH=/usr/bin}" > "$T/proc/$1.env"
}
pane() { mkdir -p "$T/panes/$1"; echo "%$3 $3" > "$T/panes/$1/$2"; }   # pane SOCKET SESSION PID
FOLDER="$HERE/skills/worker-pool/scripts/worker_claude_folder.py"
printf '{"model":"claude-opus-5","permissions":{"allow":["Bash"]}}\n' > "$CORE/settings.json"; echo '{}' > "$CORE/.claude.json"
OWN_A="$(python3 "$FOLDER" ensure "$CORE" "$A")"; OWN_B="$(python3 "$FOLDER" ensure "$CORE" "$B")"; OWN_C="$(python3 "$FOLDER" ensure "$CORE" "$C")"
proc 101 sutando-core "$CORE" "HOME=$T CLAUDE_CONFIG_DIR=$CORE PATH=/usr/bin"; pane x.sock sutando-core 101
for w in "102 $A $OWN_A" "103 $B $OWN_B" "104 $C $CORE"; do   # C has a folder of its own, but its running session predates it
  set -- $w; proc "$1" "sutando-worker-$2" "$3"; pane x.sock "sutando-worker-$2" "$1"; echo "$3" > "$T/proc/sutando-worker-$2.folder"
done
echo "$CORE" > "$T/proc/sutando-core.folder"
printf '{"model":"claude-opus-5","previous":null}\n' > "$STATE/model-switch.json"
echo '{"cards":{}}' > "$STATE/model-card-choices.json"; echo '{"attempts":[]}' > "$STATE/model-card-outcome.json"
printf '{"brain":"%s","socket":"%s","session":"sutando-core"}\n' "$CORE" "$T/x.sock" > "$T/desc.json"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log" FAKE_ROOT="$T" TMPDIR="$T/tmp" SUTANDO_TMUX_SOCKET="$T/x.sock" SUTANDO_CORE_RUNTIME=claude
unset SUTANDO_TMUX_SESSION SUTANDO_WORKER_ID SUTANDO_INSTANCE_ID
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
# run [VAR=value ...] -- <switch-model args>: the env a session would run the switch with.
run(){ local envs=(); while [ "$1" != -- ]; do envs+=("$1"); shift; done; shift; : > "$TMUX_LOG"
  env ${envs[@]+"${envs[@]}"} "$HERE/scripts/switch-model.sh" --accept-timeout 3 "$@" --state-dir "$STATE" --descriptor-file "$T/desc.json" > "$T/out" 2> "$T/err"; echo $?; }
# Everything that is the core's or shared: its record, its model-card state, its Claude folder's settings.
core_snapshot(){ { find "$STATE" -maxdepth 1 -type f ! -name '.*' | sort; cat "$STATE/model-switch.json" "$STATE/model-card-choices.json" "$STATE/model-card-outcome.json" "$CORE/settings.json"; } | cksum; }
worker_as(){ echo "SUTANDO_TMUX_SESSION=sutando-worker-$1"; echo "SUTANDO_WORKER_ID=$1"; }
field(){ python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(*[d.get(k) for k in sys.argv[2:]])" "$@" 2>/dev/null; }
CORE_BEFORE="$(core_snapshot)"; B_BEFORE="$(cat "$OWN_B/settings.json")"

# (i)+(iv) worker A, in its own folder, switches itself
rc=$(run $(worker_as "$A") -- haiku)
[ "$rc" = 0 ] && [ "$(field "$STATE/workers/$A/model-switch.json" model worker accepted)" = "haiku $A True" ] \
  && ok "1 worker A in its own folder: sent, recorded under state/workers/<A>/ (rc=0)" || fail "1" "rc=$rc $(cat "$T/err")"
grep -q -- "send-keys -t sutando-worker-$A -l /model haiku" "$TMUX_LOG" && ! grep -q -- "-t sutando-core " "$TMUX_LOG" && ! grep -q -- "sutando-worker-$B" "$TMUX_LOG" \
  && ok "2 /model went to A's pane only" || fail "2" "$(cat "$TMUX_LOG")"
[ "$(core_snapshot)" = "$CORE_BEFORE" ] && ok "3 the core's record, its model-card state and the shared settings.json are untouched" || fail "3" "core state changed"
[ ! -e "$STATE/workers/$B/model-switch.json" ] && [ "$(cat "$OWN_B/settings.json")" = "$B_BEFORE" ] && [ ! -e "$STATE/workers/$B/model" ] \
  && ok "4 worker B is untouched (no record, own settings as before)" || fail "4" "B changed"
[ "$(field "$OWN_A/settings.json" model)" = haiku ] && [ "$(cat "$STATE/workers/$A/model")" = haiku ] \
  && ok "5 the pick is in A's own folder, and its launch pick follows" || fail "5" "$(cat "$OWN_A/settings.json")"
[ "$(field "$STATE/workers/$A/model-switch.json" settings_read)" = "$OWN_A/settings.json" ] && ok "6 A's record reads A's own settings, not the core's" || fail "6" ""
rc=$(run -- sonnet --session "sutando-worker-$B")
[ "$rc" = 0 ] && [ "$(field "$STATE/workers/$B/model-switch.json" model worker)" = "sonnet $B" ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ] \
  && [ "$(field "$STATE/workers/$A/model-switch.json" model)" = haiku ] && ok "7 the core naming worker B by --session: B sent and recorded, core and A untouched" || fail "7" "rc=$rc $(cat "$T/err")"

# (ii) the folder exists, but the running session is not in it
refused15(){ [ "$rc" = 15 ] && ! grep -q send-keys "$TMUX_LOG" && [ ! -e "$STATE/workers/$C/model-switch.json" ] && [ ! -e "$STATE/workers/$C/model" ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ]; }
rc=$(run $(worker_as "$C") -- opus)
refused15 && grep -q "uses the core's Claude folder" "$T/err" && grep -q "restart it" "$T/err" \
  && ok "8 own folder exists but the running session uses the core's: exit 15, nothing sent, no record" || fail "8" "rc=$rc $(cat "$T/err")"
echo "" > "$T/proc/104.env"; rc=$(run $(worker_as "$C") -- opus)
refused15 && ok "9 the session's environment cannot be read: exit 15, nothing sent, no record" || fail "9" "rc=$rc $(cat "$T/err")"
printf 'SUTANDO_TMUX_SESSION=sutando-worker-%s CLAUDE_CONFIG_DIR=%s' "$C" "$T/elsewhere" > "$T/proc/104.env"; mkdir -p "$T/elsewhere"; rc=$(run $(worker_as "$C") -- opus)
refused15 && ok "10 the session runs in some other folder: exit 15" || fail "10" "rc=$rc $(cat "$T/err")"
echo "bash -l" > "$T/proc/104.args"; rc=$(run $(worker_as "$C") -- opus)
refused15 && ok "11 no Claude process in the session's pane: exit 15" || fail "11" "rc=$rc $(cat "$T/err")"
rm "$T/panes/x.sock/sutando-worker-$C"; rc=$(run $(worker_as "$C") -- opus)
refused15 && ok "11b no such session on the selected socket: exit 15" || fail "11b" "rc=$rc $(cat "$T/err")"
rc=$(run $(worker_as "$A") -- opus --offline)
[ "$rc" = 2 ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ] && ok "12 --offline for a worker is refused (rc=2); the core's settings are not written" || fail "12" "rc=$rc"

# only the pane on the selected socket counts; a same-named session on another tmux server never does
D=dddddddddddddddddddddddddddddddd; E=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
OWN_D="$(python3 "$FOLDER" ensure "$CORE" "$D")"; OWN_E="$(python3 "$FOLDER" ensure "$CORE" "$E")"
proc 201 "sutando-worker-$D" "$CORE"; pane x.sock "sutando-worker-$D" 201; proc 202 "sutando-worker-$D" "$OWN_D"; pane y.sock "sutando-worker-$D" 202
proc 203 "sutando-worker-$E" "$OWN_E"; pane x.sock "sutando-worker-$E" 203; proc 204 "sutando-worker-$E" "$CORE"; pane y.sock "sutando-worker-$E" 204
echo "$OWN_E" > "$T/proc/sutando-worker-$E.folder"
rc=$(run $(worker_as "$D") -- opus)
[ "$rc" = 15 ] && ! grep -q send-keys "$TMUX_LOG" && [ ! -e "$STATE/workers/$D" ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ] \
  && ok "P1a the selected socket's pane runs in the core's folder; a same-named session elsewhere in its own does not count: exit 15" || fail "P1a" "rc=$rc $(cat "$T/err")"
rc=$(run $(worker_as "$E") -- opus)
[ "$rc" = 0 ] && [ "$(field "$STATE/workers/$E/model-switch.json" model worker)" = "opus $E" ] && ! grep -q "y.sock" "$TMUX_LOG" && [ "$(core_snapshot)" = "$CORE_BEFORE" ] \
  && ok "P1b the selected socket's pane runs in its own folder; a same-named session elsewhere in the core's does not count: switched" || fail "P1b" "rc=$rc $(cat "$T/err")"
# the pane is replaced (pid 301, in the core's folder) at three points; A's record must stay as it was
proc 301 "sutando-worker-$A" "$CORE"; A_REC="$(cat "$STATE/workers/$A/model-switch.json")"
replaced(){ pane x.sock "sutando-worker-$A" 102; [ "$rc" = "$1" ] && [ "$(cat "$STATE/workers/$A/model-switch.json")" = "$A_REC" ] && [ "$(cat "$STATE/workers/$A/model")" = haiku ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ]; }
rc=$(run $(worker_as "$A") "FAKE_SWAP=panes1:sutando-worker-$A:301" -- opus)
replaced 15 && ! grep -q send-keys "$TMUX_LOG" && grep -q "before the send" "$T/err" && ok "P1c pane replaced after the check: exit 15, nothing sent, nothing recorded" || fail "P1c" "rc=$rc $(cat "$T/err")"
rc=$(run $(worker_as "$A") "FAKE_SWAP=panes2:sutando-worker-$A:301" -- opus)
replaced 7 && grep -q "WITHHELD sutando-worker-$A" "$TMUX_LOG" && ! grep -q -- "-l /model" "$TMUX_LOG" \
  && ok "P1d pane replaced between the last check and the send: the send is bound to the checked pid and withheld, nothing recorded" || fail "P1d" "rc=$rc $(cat "$T/err") $(cat "$TMUX_LOG")"
echo "$T/elsewhere" > "$T/proc/sutando-worker-$A.folder"
rc=$(run $(worker_as "$A") "FAKE_SWAP=enter:sutando-worker-$A:301" -- opus)
replaced 15 && grep -q "while waiting for acceptance" "$T/err" && ok "P1e pane replaced after the send: the read of the replaced pane is refused (exit 15), nothing recorded" || fail "P1e" "rc=$rc $(cat "$T/err")"
echo "$OWN_A" > "$T/proc/sutando-worker-$A.folder"; printf '{"model":"haiku"}\n' > "$OWN_A/settings.json"
printf '%%102 102\n%%103 103\n' > "$T/panes/x.sock/sutando-worker-$A"; rc=$(run $(worker_as "$A") -- opus)
replaced 15 && ! grep -q send-keys "$TMUX_LOG" && ok "P1f a session with more than one pane: exit 15, nothing sent" || fail "P1f" "rc=$rc $(cat "$T/err")"

# acceptance is read from the checked pane only: the same text anywhere else never counts
ACCEPTED='  ⎿  Set model to Opus 5 and saved as your default\n'
mkdir -p "$T/screen/x.sock" "$T/screen/y.sock"; pane y.sock "sutando-worker-$A" 302; proc 302 "sutando-worker-$A" "$OWN_A"
printf '%s' "$ACCEPTED" > "$T/screen/y.sock/%302"; printf '%s' "$ACCEPTED" > "$T/screen/y.sock/sutando-worker-$A"
rc=$(run $(worker_as "$A") FAKE_NO_ACCEPT=1 -- opus --accept-timeout 1)
replaced 8 && ok "P3a the acceptance line is on a same-named session's pane on another socket: not counted (exit 8), nothing recorded" || fail "P3a" "rc=$rc $(cat "$T/err")"
printf '%s' "$ACCEPTED" > "$T/screen/x.sock/sutando-worker-$A"; printf '%s' "$ACCEPTED" > "$T/screen/x.sock/%999"
rc=$(run $(worker_as "$A") FAKE_NO_ACCEPT=1 -- opus --accept-timeout 1)
replaced 8 && ! grep -q -- "capture-pane -p -t sutando-worker-$A" "$TMUX_LOG" && grep -q -- "capture-pane -p -t %102" "$TMUX_LOG" \
  && ok "P3b the acceptance line is on another pane the session name would reach: reads go to the checked pane id, not counted (exit 8)" || fail "P3b" "rc=$rc $(cat "$T/err")"
rm -rf "$T/screen" "$T/panes/y.sock/sutando-worker-$A"
printf '%s' "$ACCEPTED" > "$T/accepted"; mkdir -p "$T/screen/x.sock"; cp "$T/accepted" "$T/screen/x.sock/%102"
rc=$(run $(worker_as "$A") FAKE_NO_ACCEPT=1 "FAKE_SWAP=enter:sutando-worker-$A:301" -- opus)
replaced 15 && grep -q "while waiting for acceptance" "$T/err" && ok "P3c the acceptance line is on the pane after its process was replaced: not counted (exit 15), nothing recorded" || fail "P3c" "rc=$rc $(cat "$T/err")"
rm -rf "$T/screen"

# a worker folder that is a link is not that worker's own, wherever it points
F=ffffffffffffffffffffffffffffffff; G=0000000000000000000000000000000a; H=0000000000000000000000000000000b; WORKERS="$(dirname "$OWN_A")"
mkdir -p "$T/outside"; echo '{}' > "$T/outside/settings.json"; pid=400
linked(){ pid=$((pid+1)); ln -s "$2" "$WORKERS/$1"; proc "$pid" "sutando-worker-$1" "$WORKERS/$1"; pane x.sock "sutando-worker-$1" "$pid"; echo "$2" > "$T/proc/sutando-worker-$1.folder"
  B_NOW="$(cat "$OWN_B/settings.json")"; rc=$(run $(worker_as "$1") -- opus)
  [ "$rc" = 15 ] && ! grep -q send-keys "$TMUX_LOG" && [ ! -e "$STATE/workers/$1" ] && [ "$(cat "$OWN_B/settings.json")" = "$B_NOW" ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ]; }
linked "$F" "$OWN_B" && grep -q "is a link" "$T/err" && ok "P2a the worker's folder is a link to another worker's: exit 15, nothing sent, that worker untouched" || fail "P2a" "rc=$rc $(cat "$T/err")"
linked "$G" "$CORE" && ok "P2b the worker's folder is a link to the core's: exit 15, nothing sent" || fail "P2b" "rc=$rc $(cat "$T/err")"
linked "$H" "$T/outside" && grep -q "is a link" "$T/err" && ok "P2c the worker's folder is a link to a folder elsewhere: exit 15, nothing sent" || fail "P2c" "rc=$rc $(cat "$T/err")"
mkdir -p "$T/core2"; cp "$CORE/settings.json" "$T/core2/"; ln -s "$WORKERS" "$T/core2-workers"
printf '{"brain":"%s","socket":"%s","session":"sutando-core"}\n' "$T/core2" "$T/x.sock" > "$T/desc2.json"
: > "$TMUX_LOG"; rc=$(env SUTANDO_TMUX_SESSION="sutando-worker-$A" SUTANDO_WORKER_ID="$A" "$HERE/scripts/switch-model.sh" opus --state-dir "$STATE" --descriptor-file "$T/desc2.json" > "$T/out" 2> "$T/err"; echo $?)
replaced 15 && ! grep -q send-keys "$TMUX_LOG" && grep -q "holding the workers' Claude folders is a link" "$T/err" \
  && ok "P2d the folder holding the workers' folders is a link (a second core aliasing this one's workers): exit 15, nothing sent" || fail "P2d" "rc=$rc $(cat "$T/err")"

# (iii) a target that cannot be told is refused, never switched as the core
refused14(){ [ "$rc" = 14 ] && ! grep -q send-keys "$TMUX_LOG" && [ "$(core_snapshot)" = "$CORE_BEFORE" ]; }
rc=$(run "SUTANDO_WORKER_ID=$A" -- opus)
refused14 && grep -q "not switching the core in its place" "$T/err" && ok "13 a worker's session with no session of its own named: exit 14, the core is not switched" || fail "13" "rc=$rc $(cat "$T/err")"
rc=$(run "SUTANDO_TMUX_SESSION=sutando-worker-$A" "SUTANDO_WORKER_ID=$B" -- opus)
refused14 && ok "14 the session and the worker id disagree: exit 14" || fail "14" "rc=$rc $(cat "$T/err")"
rc=$(run "SUTANDO_TMUX_SESSION=sutando-worker-Not_An_Id" -- opus)
refused14 && ok "15 a worker session naming no worker id: exit 14" || fail "15" "rc=$rc $(cat "$T/err")"
rc=$(run -- opus --target worker); refused14 && ok "16 --target worker on the core's session: exit 14" || fail "16" "rc=$rc"
rc=$(run $(worker_as "$A") -- opus --target core); refused14 && ok "17 --target core on a worker's session: exit 14" || fail "17" "rc=$rc"

# --dry-run names the session kind, the record and whether it would refuse
rc=$(run $(worker_as "$A") -- opus --dry-run)
[ "$rc" = 0 ] && grep -q "kind=worker ($A" "$T/out" && grep -q "state/workers/$A/model-switch.json" "$T/out" && grep -q "would not refuse" "$T/out" \
  && ! grep -q send-keys "$TMUX_LOG" && [ "$(cat "$STATE/workers/$A/model-switch.json")" = "$A_REC" ] && ok "18 dry-run, worker in its own folder: kind, record path, would not refuse" || fail "18" "$(cat "$T/out")"
rc=$(run "SUTANDO_TMUX_SESSION=sutando-worker-$C" -- opus --dry-run)
[ "$rc" = 0 ] && grep -q "kind=worker ($C" "$T/out" && grep -q "would refuse (exit 15)" "$T/out" && ok "19 dry-run, worker not in its own folder: would refuse (exit 15)" || fail "19" "$(cat "$T/out")"
rc=$(run "SUTANDO_WORKER_ID=$A" -- opus --dry-run)
[ "$rc" = 0 ] && grep -q "kind=unidentified" "$T/out" && grep -q "would refuse (exit 14)" "$T/out" && ok "20 dry-run, target not identified: would refuse (exit 14)" || fail "20" "$(cat "$T/out")"
rc=$(run -- opus --dry-run)
[ "$rc" = 0 ] && grep -q "kind=core; record $STATE/model-switch.json" "$T/out" && grep -q "previous read from $CORE/settings.json" "$T/out" \
  && [ "$(core_snapshot)" = "$CORE_BEFORE" ] && ok "21 dry-run, core: kind=core and the core's record path" || fail "21" "$(cat "$T/out")"

# (v) the core's own switch is as before
rc=$(run -- haiku)
[ "$rc" = 0 ] && [ "$(field "$STATE/model-switch.json" model previous previous_source accepted worker settings_read)" = "haiku claude-opus-5 settings.json True None $CORE/settings.json" ] \
  && grep -q -- "send-keys -t sutando-core -l /model haiku" "$TMUX_LOG" && ! grep -q "sutando-worker-" "$TMUX_LOG" \
  && [ "$(field "$STATE/workers/$A/model-switch.json" model)" = haiku ] && [ "$(field "$STATE/workers/$B/model-switch.json" model)" = sonnet ] \
  && ok "22 core: sent to the core's pane, recorded in state/model-switch.json, no worker record touched" || fail "22" "rc=$rc $(cat "$T/err")"
rc=$(run "SUTANDO_WORKER_ID=$A" -- opus --target core)
[ "$rc" = 0 ] && [ "$(field "$STATE/model-switch.json" model)" = opus ] && ok "23 --target core from a worker's session switches the core on purpose" || fail "23" "rc=$rc $(cat "$T/err")"
[ "$fails" = 0 ] && echo "PASS: switch-model worker target" || { echo "FAILED: $fails"; exit 1; }
