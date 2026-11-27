#!/usr/bin/env bash
# A pool worker's session is switched and recorded per worker only while it RUNS in a Claude folder of its own.
# tmux, pgrep and ps are PATH shims over temp folders; the fake CLI saves /model into the folder the session runs in.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; export TMUX_FAKE_UNWRAP="$HERE/tests/lib/tmux-fake-unwrap.sh"
T="$(cd "$(mktemp -d)" && pwd -P)"; trap 'rm -rf "$T"' EXIT
A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; B=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; C=cccccccccccccccccccccccccccccccc
CORE="$T/claude-core"; STATE="$T/ws/state"
mkdir -p "$T/bin" "$T/tmp" "$T/proc" "$CORE/skills" "$STATE/workers/$A" "$STATE/workers/$B"
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
. "$TMUX_FAKE_UNWRAP"
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in *" list-windows "*) echo 0; exit 0;; *" list-panes "*) exit 0;; esac
case " $* " in *" capture-pane "*)
  # One acceptance line per /model sent; the pick is saved in the folder that session runs in.
  acc=""; sent="$(grep -- "-l /model" "$TMUX_LOG" | tail -1)"
  if [ -n "$sent" ]; then
    model="${sent##*-l /model }"; session="$(printf '%s' "$sent" | sed 's/.*send-keys -t \([^ ]*\) .*/\1/')"
    case "$model" in opus) name="Opus 5";; haiku) name="Haiku 4.5";; sonnet) name="Sonnet 5";; *) name="$model";; esac
    acc="  ⎿  Set model to $name and saved as your default\n"
    folder="$(cat "$FAKE_ROOT/proc/$session.folder" 2>/dev/null)"
    [ -n "$folder" ] && printf '{"model":"%s"}\n' "$model" > "$folder/settings.json"
  fi
  printf '%b' "$acc────\n❯ \n────\n";; esac
exit 0
SH
cat > "$T/bin/pgrep" <<'SH'
#!/usr/bin/env bash
case " $* " in *" -ax claude "*) cat "$FAKE_ROOT/proc/list" 2>/dev/null; exit 0;; esac
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
# A fake running session: its pid, its argv, and the environment its process was started with.
proc() { # proc PID SESSION FOLDER [ENV-OVERRIDE]
  echo "$1 claude --name $2" >> "$T/proc/list"; echo "claude --name $2 --dangerously-skip-permissions" > "$T/proc/$1.args"
  printf '%s' "${4-HOME=$T SUTANDO_TMUX_SESSION=$2 CLAUDE_CONFIG_DIR=$3 PATH=/usr/bin}" > "$T/proc/$1.env"; echo "$3" > "$T/proc/$2.folder"
}
FOLDER="$HERE/skills/worker-pool/scripts/worker_claude_folder.py"
printf '{"model":"claude-opus-5","permissions":{"allow":["Bash"]}}\n' > "$CORE/settings.json"; echo '{}' > "$CORE/.claude.json"
OWN_A="$(python3 "$FOLDER" ensure "$CORE" "$A")"; OWN_B="$(python3 "$FOLDER" ensure "$CORE" "$B")"; OWN_C="$(python3 "$FOLDER" ensure "$CORE" "$C")"
proc 101 sutando-core "$CORE" "HOME=$T CLAUDE_CONFIG_DIR=$CORE PATH=/usr/bin"
proc 102 "sutando-worker-$A" "$OWN_A"; proc 103 "sutando-worker-$B" "$OWN_B"
proc 104 "sutando-worker-$C" "$CORE"      # C has a folder of its own, but its running session predates it
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
grep -v "^104 " "$T/proc/list" > "$T/proc/list.new"; mv "$T/proc/list.new" "$T/proc/list"; rc=$(run $(worker_as "$C") -- opus)
refused15 && ok "11 no running process for the session: exit 15" || fail "11" "rc=$rc $(cat "$T/err")"
rc=$(run $(worker_as "$A") -- opus --offline)
[ "$rc" = 2 ] && [ "$(core_snapshot)" = "$CORE_BEFORE" ] && ok "12 --offline for a worker is refused (rc=2); the core's settings are not written" || fail "12" "rc=$rc"

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
A_REC="$(cat "$STATE/workers/$A/model-switch.json")"
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
