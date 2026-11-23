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
S=$(python3 -c "import json;print(json.load(open('$T/state/model-switch.json'))['status'])")
[ "$rc" = 12 ] && [ "$R" = "gpt-6-luna codex False next-turn None" ] && [ "$S" = pending ] && grep -q "next task" "$T/err" \
  && grep -q "not switched yet" "$T/err" && ! grep -q "^switched" "$T/out" \
  && ok "5 app-server seat: rc=12 pending, recorded core-only, never said switched" || fail "5" "rc=$rc R=$R S=$S $(cat "$T/err")"
rc=$(TMUX_PANES="$TUI" run gpt-5.6-terra); R=$(rec)
[ "$rc" = 11 ] && [ "$R" = "gpt-5.6-terra codex False next-start gpt-6-luna" ] && grep -q "next start" "$T/err" \
  && ok "6 Codex TUI: rc=11, saved for the core's next start, previous kept" || fail "6" "rc=$rc R=$R $(cat "$T/err")"
rc=$(run gpt-5.6-luna); R=$(rec)
[ "$rc" = 13 ] && [ "$R" = "gpt-5.6-luna codex False next-start gpt-5.6-terra" ] && grep -q "applies at the next core start" "$T/err" \
  && ! grep -q "^switched" "$T/out" \
  && ok "7 no core running: rc=13, saved, applies at the next core start, never said switched" || fail "7" "rc=$rc R=$R $(cat "$T/err")"
! grep -q "send-keys" "$TMUX_LOG" && ok "8 nothing is typed into any pane" || fail "8" "$(cat "$TMUX_LOG")"
[ "$(cat "$T/codex-home/config.toml")" = "$HOME_BEFORE" ] && ok "9 the shared CODEX_HOME config.toml (the workers' model) is untouched" || fail "9" "$(cat "$T/codex-home/config.toml")"

# The core launcher reads the record; a Claude core's record (no runtime) is never a Codex model.
M=$(python3 "$HERE/src/codex_core_model.py" current --state-dir "$T/state"); [ "$M" = gpt-5.6-luna ] && ok "10 current names the recorded Codex model" || fail "10" "$M"
printf '{"model":"opus","accepted":true}\n' > "$T/state/model-switch.json"
M=$(python3 "$HERE/src/codex_core_model.py" current --state-dir "$T/state"); [ -z "$M" ] && ok "11 a Claude core's record is not read as a Codex model" || fail "11" "$M"

# The text names the model the core is on (the last one Codex took), not the last pick saved.
# VM 2026-11-23: with Sol working and Astra pending, a pick of Luna said "(was astra) … stays on astra".
python3 "$HERE/src/codex_core_model.py" save --state-dir "$T/state" --model gpt-6.2-sol --applied next-turn --accepted > /dev/null
rc=$(TMUX_PANES="$SEAT" run gpt-6.2-astra)
rc=$(TMUX_PANES="$SEAT" run gpt-6-luna)
[ "$rc" = 12 ] && grep -q "pending model=gpt-6-luna (the core is on gpt-6.2-sol)" "$T/err" && grep -q "until then it stays on gpt-6.2-sol;" "$T/err" \
  && ! grep -q "astra" "$T/err" && ok "12 a pending pick over a pending pick: the text says the working model, never the untaken pick" || fail "12" "rc=$rc $(cat "$T/err")"
rc=$(TMUX_PANES="$TUI" run gpt-5.6-terra)
[ "$rc" = 11 ] && grep -q "(the core is on gpt-6.2-sol)" "$T/err" && ok "13 the TUI's text names the working model too" || fail "13" "rc=$rc $(cat "$T/err")"
rm -f "$T/state/model-switch.json"; rc=$(run gpt-5.6-luna)
[ "$rc" = 13 ] && grep -q "(the core was last on its launch model)" "$T/err" && ok "14 with no record, it says the launch model rather than a dash" || fail "14" "rc=$rc $(cat "$T/err")"

echo; [ $fails -eq 0 ] && echo "switch-model-codex: all 14 checks pass" || { echo "switch-model-codex: $fails FAILED"; exit 1; }
