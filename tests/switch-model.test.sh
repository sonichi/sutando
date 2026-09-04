#!/usr/bin/env bash
# switch-model.sh: pins settings.json, records the switch with a timestamp,
# sends /model to the live pane — and refuses names the CLI would not accept.
# tmux is a PATH shim that logs its argv; no real pane is touched.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/cfg" "$T/state"
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
[ -n "${TMUX_FAIL:-}" ] && exit 1
case " $* " in *" capture-pane "*) printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}";; esac
exit 0
SH
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log" CLAUDE_CONFIG_DIR="$T/cfg" SUTANDO_TMUX_SOCKET="/tmp/sutando-tmux.sock"
printf '{"model":"claude-opus-5","permissions":{"allow":["Bash"]}}\n' > "$T/cfg/settings.json"
fails=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1 — $2"; fails=$((fails+1)); }
run() { "$HERE/scripts/switch-model.sh" "$@" --state-dir "$T/state" --brain "$T/cfg" > "$T/out" 2> "$T/err"; echo $?; }
runraw() { "$HERE/scripts/switch-model.sh" "$@" --brain "$T/cfg" > "$T/out" 2> "$T/err"; echo $?; }

rc=$(run 'gpt-5; rm -rf /'); [ "$rc" = 2 ] && ok "1 a non-claude name is refused (rc=2)" || fail "1 refused rc" "$rc"
[ ! -e "$T/tmux.log" ] && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = claude-opus-5 ] \
  && ok "2 refusal writes nothing and sends nothing" || fail "2 side effects on refusal" "$(cat "$T/tmux.log" 2>/dev/null)"

rc=$(run 'claude-fable-5-1[1m]'); [ "$rc" = 0 ] && ok "3 a full id with context tag switches (rc=0)" || fail "3 rc" "$rc $(cat "$T/err")"
M=$(python3 -c "import json;d=json.load(open('$T/cfg/settings.json'));print(d['model'],d['permissions']['allow'][0])")
[ "$M" = "claude-fable-5-1[1m] Bash" ] && ok "4 settings.json pinned, other keys intact" || fail "4 settings" "$M"
R=$(python3 -c "import json;d=json.load(open('$T/state/model-switch.json'));print(d['model'],d['previous'],d['ts'][:4],d['by'])")
case "$R" in "claude-fable-5-1[1m] claude-opus-5 20"*" skills/model-switch/scripts/switch-model.sh") ok "5 switch record carries model, previous, a dated ts, and the writer";; *) fail "5 record" "$R";; esac
grep -q -- "send-keys -t sutando-core -l /model claude-fable-5-1\[1m\]" "$T/tmux.log" && grep -q -- "send-keys -t sutando-core Enter" "$T/tmux.log" \
  && ok "6 the live pane got '/model <id>' then Enter, literal (-l)" || fail "6 tmux argv" "$(cat "$T/tmux.log")"
grep -q -- "-S /tmp/sutando-tmux.sock" "$T/tmux.log" && ok "7 targets the core's socket (descriptor/env) by default" || fail "7 socket" "$(head -1 "$T/tmux.log")"

: > "$T/tmux.log"; rc=$(TMUX_FAIL=1 run opus); [ "$rc" = 3 ] && ok "8 no live session: pin applied, exit 3 says the live half did not happen" || fail "8 rc" "$rc"
[ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] && ok "9 ...and the pin still landed (alias accepted)" || fail "9 pin" "$(cat "$T/cfg/settings.json")"
grep -q "NOT sent" "$T/err" && ok "10 ...and it says so on stderr" || fail "10 stderr" "$(cat "$T/err")"

: > "$T/tmux.log"; rc=$(run sonnet --dry-run); [ "$rc" = 0 ] && ! grep -q send-keys "$T/tmux.log" \
  && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] \
  && ok "11 --dry-run changes nothing and sends nothing" || fail "11 dry-run" "rc=$rc $(cat "$T/tmux.log")"

# --- partial failure: the reviewer's case — a state path that is a FILE
: > "$T/tmux.log"; touch "$T/statefile"; rc=$(runraw sonnet --state-dir "$T/statefile")
[ "$rc" = 1 ] && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] \
  && ok "12 unwritable record: exit 1 and settings.json UNCHANGED (record is written first)" || fail "12 partial failure" "rc=$rc model=$(cat "$T/cfg/settings.json")"
! grep -q send-keys "$T/tmux.log" && grep -q "nothing changed" "$T/err" && ok "13 ...nothing sent, and the message says nothing changed" || fail "13 partial msg" "$(cat "$T/err")"
! grep -Eq '(^|[^a-z-])python3( |$)' "$HERE/skills/model-switch/scripts/switch-model.sh" && grep -q 'python-bin' "$HERE/skills/model-switch/scripts/switch-model.sh" \
  && ok "14 no bare python3; python resolved through sutando-config.sh python-bin" || fail "14 python resolver" "$(grep -nE 'python3( |$)' "$HERE/skills/model-switch/scripts/switch-model.sh")"
grep -q 'sutando-config.sh" runtime' "$HERE/skills/model-switch/scripts/switch-model.sh" && ! grep -q 'tmux-socket' "$HERE/skills/model-switch/scripts/switch-model.sh" \
  && ok "15 brain/socket/session default from the runtime descriptor, not the ambient tmux-socket getter" || fail "15 descriptor" ""

# --- the input box is read before anything is written
: > "$T/tmux.log"; rc=$(TMUX_PANE_TEXT='────\n❯ half-typed message\n────\n' run haiku)
[ "$rc" = 5 ] && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] && ! grep -q send-keys "$T/tmux.log" \
  && ok "16 pending text in the input box: exit 5, nothing pinned, nothing sent" || fail "16 input box" "rc=$rc $(cat "$T/tmux.log")"
grep -q "half-typed" "$T/err" && ok "17 ...and the refusal quotes the pending text" || fail "17 quote" "$(cat "$T/err")"
: > "$T/tmux.log"; rc=$(TMUX_PANE_TEXT='✽ Thinking… (12s)\n────\n❯\xc2\xa0\n────\n' run haiku)
[ "$rc" = 0 ] && grep -q "send-keys -t sutando-core -l /model haiku" "$T/tmux.log" && ok "18 a clear prompt (nbsp after ❯, turn running) still sends — the CLI queues it" || fail "18 clear prompt" "rc=$rc $(cat "$T/err")"
# --- Claude Code only
: > "$T/tmux.log"; rc=$(SUTANDO_CORE_RUNTIME=codex run opus)
[ "$rc" = 4 ] && ! grep -q send-keys "$T/tmux.log" && grep -q "start-cli.sh --restart" "$T/err" \
  && ok "19 codex runtime: exit 4, nothing sent, the restart path named" || fail "19 codex" "rc=$rc $(cat "$T/err")"

# --- the reviewer's case: an EXISTING record must survive a failed pin
python3 -c "import json;json.dump({'model':'claude-opus-5','previous':None,'ts':'T0','by':'prior'},open('$T/state/model-switch.json','w'))"
mv "$T/cfg/settings.json" "$T/cfg/settings.bak"; mkdir "$T/cfg/settings.json"   # a directory forces the replace to fail
rc=$(run sonnet); rmdir "$T/cfg/settings.json"; mv "$T/cfg/settings.bak" "$T/cfg/settings.json"
[ "$rc" = 1 ] && [ "$(python3 -c "import json;print(json.load(open('$T/state/model-switch.json'))['by'])")" = prior ] \
  && ok "20 failed pin: the prior switch record is byte-intact (staged record discarded)" || fail "20 prior record" "rc=$rc $(cat "$T/state/model-switch.json" 2>&1 | head -c 120)"
grep -q "prior switch record kept" "$T/err" && ok "21 ...and the message says the prior record was kept" || fail "21 msg" "$(cat "$T/err")"
[ -z "$(ls -A "$T/state" | grep staged)" ] && ok "22 ...and no staged file is left behind" || fail "22 staged leftover" "$(ls -A "$T/state")"

# --- the record path is a DIRECTORY: the pin must not survive unrecorded
rm -f "$T/state/model-switch.json"; mkdir -p "$T/state/model-switch.json"; : > "$T/tmux.log"; PRIOR="$(cat "$T/cfg/settings.json")"
rc=$(run sonnet); rmdir "$T/state/model-switch.json"
[ "$rc" = 1 ] && [ "$(cat "$T/cfg/settings.json")" = "$PRIOR" ] && ! grep -q send-keys "$T/tmux.log" \
  && ok "23 record commit fails after the pin: settings rolled back to the prior bytes, nothing sent" || fail "23 rollback" "rc=$rc $(cat "$T/cfg/settings.json")"
grep -q "rolled back" "$T/err" && ok "24 ...and the message says so" || fail "24 msg" "$(cat "$T/err")"
# --- a descriptor whose paths carry spaces must survive the JSON->shell hop
mkdir -p "$T/Brain With Spaces"; printf '{"model":"claude-opus-5"}\n' > "$T/Brain With Spaces/settings.json"
printf '{"brain":"%s","socket":"/private/tmp/socket path/s.sock","session":"core name"}\n' "$T/Brain With Spaces" > "$T/desc.json"
rc=$(env -u SUTANDO_TMUX_SOCKET -u SUTANDO_TMUX_SESSION "$HERE/scripts/switch-model.sh" haiku --dry-run --descriptor-file "$T/desc.json" --state-dir "$T/state" > "$T/out" 2> "$T/err"; echo $?)
grep -q "Brain With Spaces/settings.json" "$T/out" && grep -q -- "-S /private/tmp/socket path/s.sock -t core name" "$T/out" \
  && ok "25 descriptor paths with spaces reach brain/socket/session intact" || fail "25 spaces" "$(cat "$T/out")"

# --- two overlapping switches must linearize: settings, record and the live command agree
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in *" capture-pane "*) [ -n "${TMUX_CAP_DELAY:-}" ] && sleep "$TMUX_CAP_DELAY"; printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}";; esac
exit 0
SH
chmod +x "$T/bin/tmux"; : > "$T/tmux.log"; printf '{"model":"opus"}\n' > "$T/cfg/settings.json"
(TMUX_CAP_DELAY=0.5 "$HERE/scripts/switch-model.sh" sonnet --state-dir "$T/state" --brain "$T/cfg" >/dev/null 2>&1) & sleep 0.1
("$HERE/scripts/switch-model.sh" haiku --state-dir "$T/state" --brain "$T/cfg" >/dev/null 2>&1) & wait
FINAL=$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])"); REC=$(python3 -c "import json;print(json.load(open('$T/state/model-switch.json'))['model'])")
LASTSEND=$(grep -- "-l /model" "$T/tmux.log" | tail -1 | sed 's/.*\/model //')
[ "$FINAL" = "$REC" ] && [ "$FINAL" = "$LASTSEND" ] && ok "26 overlapping switches linearize: settings=$FINAL record=$REC last-send=$LASTSEND agree" || fail "26 race" "settings=$FINAL record=$REC last-send=$LASTSEND"
SEQ="$(grep send-keys "$T/tmux.log" | sed -E 's/.*-l \/model (sonnet|haiku)$/lit:\1/; s/.*Enter$/enter/' | tr '\n' ' ')"
case "$SEQ" in "lit:sonnet enter lit:haiku enter "|"lit:haiku enter lit:sonnet enter ") ok "27 ...and the live sends do not interleave";; *) fail "27 interleave" "$SEQ";; esac

echo; [ $fails -eq 0 ] && echo "switch-model: all 27 checks pass" || { echo "switch-model: $fails FAILED"; exit 1; }
