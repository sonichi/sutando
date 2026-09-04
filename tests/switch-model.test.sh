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
exit 0
SH
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log" CLAUDE_CONFIG_DIR="$T/cfg" SUTANDO_TMUX_SOCKET="/tmp/sutando-tmux.sock"
printf '{"model":"claude-opus-5","permissions":{"allow":["Bash"]}}\n' > "$T/cfg/settings.json"
fails=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1 — $2"; fails=$((fails+1)); }
run() { "$HERE/scripts/switch-model.sh" "$@" --state-dir "$T/state" > "$T/out" 2> "$T/err"; echo $?; }
runraw() { "$HERE/scripts/switch-model.sh" "$@" > "$T/out" 2> "$T/err"; echo $?; }

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
grep -q -- "-S /tmp/sutando-tmux.sock" "$T/tmux.log" && ok "7 targets the core's socket by default" || fail "7 socket" "$(head -1 "$T/tmux.log")"

: > "$T/tmux.log"; rc=$(TMUX_FAIL=1 run opus); [ "$rc" = 3 ] && ok "8 no live session: pin applied, exit 3 says the live half did not happen" || fail "8 rc" "$rc"
[ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] && ok "9 ...and the pin still landed (alias accepted)" || fail "9 pin" "$(cat "$T/cfg/settings.json")"
grep -q "NOT sent" "$T/err" && ok "10 ...and it says so on stderr" || fail "10 stderr" "$(cat "$T/err")"

: > "$T/tmux.log"; rc=$(run sonnet --dry-run); [ "$rc" = 0 ] && [ ! -s "$T/tmux.log" ] \
  && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] \
  && ok "11 --dry-run changes nothing and sends nothing" || fail "11 dry-run" "rc=$rc $(cat "$T/tmux.log")"

# --- partial failure: the reviewer's case — a state path that is a FILE
: > "$T/tmux.log"; touch "$T/statefile"; rc=$(runraw sonnet --state-dir "$T/statefile")
[ "$rc" = 1 ] && [ "$(python3 -c "import json;print(json.load(open('$T/cfg/settings.json'))['model'])")" = opus ] \
  && ok "12 unwritable record: exit 1 and settings.json UNCHANGED (record is written first)" || fail "12 partial failure" "rc=$rc model=$(cat "$T/cfg/settings.json")"
[ ! -s "$T/tmux.log" ] && grep -q "nothing changed" "$T/err" && ok "13 ...nothing sent, and the message says nothing changed" || fail "13 partial msg" "$(cat "$T/err")"
! grep -Eq '(^|[^a-z-])python3( |$)' "$HERE/skills/model-switch/scripts/switch-model.sh" && grep -q 'python-bin' "$HERE/skills/model-switch/scripts/switch-model.sh" \
  && ok "14 no bare python3; python resolved through sutando-config.sh python-bin" || fail "14 python resolver" "$(grep -nE 'python3( |$)' "$HERE/skills/model-switch/scripts/switch-model.sh")"
grep -q 'tmux-socket' "$HERE/skills/model-switch/scripts/switch-model.sh" && ok "15 socket resolved through sutando-config.sh tmux-socket when unset" || fail "15 socket resolver" ""

echo; [ $fails -eq 0 ] && echo "switch-model: all 15 checks pass" || { echo "switch-model: $fails FAILED"; exit 1; }
