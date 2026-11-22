#!/usr/bin/env bash
# switch-model.sh on a Codex core: the pick is core-only (state/model-switch.json, runtime codex),
# never written to the shared CODEX_HOME's config.toml, nothing is typed into a pane, and the
# outcome depends on what serves the core (app-server seat: next task; TUI: next start; none:
# next launch). tmux is a PATH shim that answers list-panes from TMUX_PANES.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/state" "$T/codex-home"
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in *" list-panes "*) printf '%b' "${TMUX_PANES:-}"; exit 0;; esac
exit 0
SH
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log" SUTANDO_TMUX_SOCKET="$T/s.sock" SUTANDO_TMUX_SESSION=sutando-core
export SUTANDO_CORE_RUNTIME=codex CODEX_HOME="$T/codex-home"
printf 'model = "gpt-worker"\n' > "$T/codex-home/config.toml"; HOME_BEFORE="$(cat "$T/codex-home/config.toml")"
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
run(){ : > "$TMUX_LOG"; "$HERE/scripts/switch-model.sh" "$@" --state-dir "$T/state" > "$T/out" 2> "$T/err"; echo $?; }
rec(){ python3 -c "import json;d=json.load(open('$T/state/model-switch.json'));print(d['model'],d['runtime'],d['accepted'],d['applied'],d['previous'])"; }
SEAT='%1 0 codex-seat --host x\n'; TUI='%1 0 codex -C /repo\n'

rc=$(run opus); [ "$rc" = 2 ] && [ ! -e "$T/state/model-switch.json" ] && ok "1 a Claude alias is not a Codex model: rc=2, nothing recorded" || fail "1" "rc=$rc $(cat "$T/err")"
rc=$(run 'gpt-5; rm -rf /'); [ "$rc" = 2 ] && [ ! -e "$T/state/model-switch.json" ] && ok "2 a name with shell text is refused: rc=2" || fail "2" "rc=$rc"
rc=$(run -- '-m'); [ "$rc" = 2 ] && ok "3 a flag-looking name is refused" || fail "3" "rc=$rc"
rc=$(run gpt-6-luna --dry-run); [ "$rc" = 0 ] && [ ! -e "$T/state/model-switch.json" ] && grep -q "core-only" "$T/out" && ok "4 --dry-run writes nothing" || fail "4" "rc=$rc $(cat "$T/out")"

rc=$(TMUX_PANES="$SEAT" run gpt-6-luna); R=$(rec)
[ "$rc" = 0 ] && [ "$R" = "gpt-6-luna codex True next-turn None" ] && grep -q "next task" "$T/out" \
  && ok "5 app-server seat: rc=0, recorded core-only for the next turn" || fail "5" "rc=$rc R=$R $(cat "$T/err")"
rc=$(TMUX_PANES="$TUI" run gpt-5.6-terra); R=$(rec)
[ "$rc" = 11 ] && [ "$R" = "gpt-5.6-terra codex False next-start gpt-6-luna" ] && grep -q "next start" "$T/err" \
  && ok "6 Codex TUI: rc=11, saved for the core's next start, previous kept" || fail "6" "rc=$rc R=$R $(cat "$T/err")"
rc=$(run gpt-5.6-luna); R=$(rec)
[ "$rc" = 0 ] && [ "$R" = "gpt-5.6-luna codex False next-start gpt-5.6-terra" ] && grep -q "next launch" "$T/out" \
  && ok "7 no core running: rc=0, saved for its next launch" || fail "7" "rc=$rc R=$R $(cat "$T/err")"
! grep -q "send-keys" "$TMUX_LOG" && ok "8 nothing is typed into any pane" || fail "8" "$(cat "$TMUX_LOG")"
[ "$(cat "$T/codex-home/config.toml")" = "$HOME_BEFORE" ] && ok "9 the shared CODEX_HOME config.toml (the workers' model) is untouched" || fail "9" "$(cat "$T/codex-home/config.toml")"

# The core launcher reads the record; a Claude core's record (no runtime) is never a Codex model.
M=$(python3 "$HERE/src/codex_core_model.py" current --state-dir "$T/state"); [ "$M" = gpt-5.6-luna ] && ok "10 current names the recorded Codex model" || fail "10" "$M"
printf '{"model":"opus","accepted":true}\n' > "$T/state/model-switch.json"
M=$(python3 "$HERE/src/codex_core_model.py" current --state-dir "$T/state"); [ -z "$M" ] && ok "11 a Claude core's record is not read as a Codex model" || fail "11" "$M"

echo; [ $fails -eq 0 ] && echo "switch-model-codex: all 11 checks pass" || { echo "switch-model-codex: $fails FAILED"; exit 1; }
