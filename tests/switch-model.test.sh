#!/usr/bin/env bash
# switch-model.sh: records the switch, preflights the pane, types /model through
# the shared sender. It NEVER writes settings.json — the CLI persists /model
# itself. tmux is a PATH shim; python is the real resolver.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/cfg" "$T/state"
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
[ -n "${TMUX_FAIL:-}" ] && exit 1
case " $* " in *" capture-pane "*) [ -n "${TMUX_CAP_DELAY:-}" ] && sleep "$TMUX_CAP_DELAY"; printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}";; esac
exit 0
SH
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log" SUTANDO_TMUX_SOCKET="/tmp/sutando-tmux.sock"
printf '{"model":"claude-opus-5","permissions":{"allow":["Bash"]}}\n' > "$T/cfg/settings.json"; SETTINGS_BEFORE="$(cat "$T/cfg/settings.json")"
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
run(){ : > "$TMUX_LOG"; "$HERE/scripts/switch-model.sh" "$@" --state-dir "$T/state" --brain "$T/cfg" > "$T/out" 2> "$T/err"; echo $?; }
settings_untouched(){ [ "$(cat "$T/cfg/settings.json")" = "$SETTINGS_BEFORE" ]; }

rc=$(run 'gpt-5; rm -rf /'); [ "$rc" = 2 ] && ! grep -q send-keys "$TMUX_LOG" && [ ! -e "$T/state/model-switch.json" ] && ok "1 a non-claude name is refused (rc=2), nothing written or sent" || fail "1" "rc=$rc"
rc=$(run 'claude-fable-5-1[1m]'); [ "$rc" = 0 ] && ok "2 a full id with context tag switches (rc=0)" || fail "2" "rc=$rc $(cat "$T/err")"
settings_untouched && ok "3 settings.json is NEVER written (the CLI persists /model itself)" || fail "3 settings written" "$(cat "$T/cfg/settings.json")"
R=$(python3 -c "import json;d=json.load(open('$T/state/model-switch.json'));print(d['model'],d['previous'],d['ts'][:2],d['by'])")
case "$R" in "claude-fable-5-1[1m] claude-opus-5 20 skills/model-switch/scripts/switch-model.sh") ok "4 record carries model, previous (read from settings), a dated ts, the writer";; *) fail "4 record" "$R";; esac
grep -q -- "send-keys -t sutando-core -l /model claude-fable-5-1\[1m\]" "$TMUX_LOG" && grep -q -- "send-keys -t sutando-core Enter" "$TMUX_LOG" && ok "5 the live pane got '/model <id>' then Enter, literal, via the shared sender" || fail "5 tmux argv" "$(cat "$TMUX_LOG")"
grep -q -- "-S /tmp/sutando-tmux.sock" "$TMUX_LOG" && ok "6 targets the core's socket (descriptor/env)" || fail "6 socket" ""
rc=$(TMUX_FAIL=1 run opus); [ "$rc" = 3 ] && ! grep -q send-keys "$TMUX_LOG" && ok "7 no live session: exit 3, nothing sent" || fail "7" "rc=$rc"
rc=$(run sonnet --dry-run); [ "$rc" = 0 ] && ! grep -q send-keys "$TMUX_LOG" && ok "8 --dry-run sends nothing" || fail "8" "rc=$rc"
touch "$T/statefile"; rc=$("$HERE/scripts/switch-model.sh" sonnet --state-dir "$T/statefile" --brain "$T/cfg" > "$T/out" 2> "$T/err"; echo $?)
[ "$rc" = 1 ] && grep -q "nothing changed" "$T/err" && settings_untouched && ok "9 unwritable state path: exit 1, 'nothing changed', settings untouched" || fail "9" "rc=$rc $(cat "$T/err")"
rc=$(TMUX_PANE_TEXT='────\n❯ half-typed message\n────\n' run haiku); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && grep -q "half-typed" "$T/err" && ok "10 pending text in the input box: exit 5, nothing sent, text quoted" || fail "10" "rc=$rc $(cat "$T/err")"
rc=$(TMUX_PANE_TEXT='✽ Thinking… (12s)\n────\n❯\xc2\xa0\n────\n' run haiku); [ "$rc" = 0 ] && grep -q "send-keys -t sutando-core -l /model haiku" "$TMUX_LOG" && ok "11 a clear prompt (nbsp, turn running) still sends — the CLI queues it" || fail "11" "rc=$rc $(cat "$T/err")"
rc=$(SUTANDO_CORE_RUNTIME=codex run opus); [ "$rc" = 4 ] && ! grep -q send-keys "$TMUX_LOG" && grep -q "start-cli.sh --restart" "$T/err" && ok "12 codex runtime: exit 4, restart path named" || fail "12" "rc=$rc"
mkdir -p "$T/Brain With Spaces"; printf '{"model":"claude-opus-5"}\n' > "$T/Brain With Spaces/settings.json"
printf '{"brain":"%s","socket":"/private/tmp/socket path/s.sock","session":"core name"}\n' "$T/Brain With Spaces" > "$T/desc.json"
rc=$(env -u SUTANDO_TMUX_SOCKET -u SUTANDO_TMUX_SESSION "$HERE/scripts/switch-model.sh" haiku --dry-run --descriptor-file "$T/desc.json" --state-dir "$T/state" > "$T/out" 2> "$T/err"; echo $?)
grep -q "Brain With Spaces/settings.json" "$T/out" && grep -q -- "-S /private/tmp/socket path/s.sock -t core name" "$T/out" && ok "13 descriptor paths with spaces reach brain/socket/session intact" || fail "13" "$(cat "$T/out")"
! grep -Eq '(^|[^a-z-])python3( |$)' "$HERE/skills/model-switch/scripts/switch-model.sh" && grep -q 'python-bin' "$HERE/skills/model-switch/scripts/switch-model.sh" && ok "14 no bare python3; python via python-bin" || fail "14" ""
! grep -q "capture-pane\|send-keys" "$HERE/skills/model-switch/scripts/switch-model.sh" && ok "15 the script touches no tmux itself — the shared sender does" || fail "15" ""
# overlapping switches linearize: record and last live command agree, sends do not interleave
: > "$TMUX_LOG"; (TMUX_CAP_DELAY=0.5 "$HERE/scripts/switch-model.sh" sonnet --state-dir "$T/state" --brain "$T/cfg" >/dev/null 2>&1) & sleep 0.1
("$HERE/scripts/switch-model.sh" haiku --state-dir "$T/state" --brain "$T/cfg" >/dev/null 2>&1) & wait
REC=$(python3 -c "import json;print(json.load(open('$T/state/model-switch.json'))['model'])"); LASTSEND=$(grep -- "-l /model" "$TMUX_LOG" | tail -1 | sed 's/.*\/model //')
[ "$REC" = "$LASTSEND" ] && ok "16 overlapping switches linearize: record=$REC last-send=$LASTSEND" || fail "16 race" "record=$REC last-send=$LASTSEND"
SEQ="$(grep send-keys "$TMUX_LOG" | sed -E 's/.*-l \/model (sonnet|haiku)$/lit:\1/; s/.*Enter$/enter/' | tr '\n' ' ')"
case "$SEQ" in "lit:sonnet enter lit:haiku enter "|"lit:haiku enter lit:sonnet enter ") ok "17 ...and the live sends do not interleave";; *) fail "17" "$SEQ";; esac
: > "$TMUX_LOG"; (TMUX_CAP_DELAY=0.5 "$HERE/scripts/switch-model.sh" sonnet --state-dir "$T/state" --brain "$T/cfg" --socket "$T/x.sock" >/dev/null 2>&1) & sleep 0.15
(bash "$HERE/scripts/tmux-send-line.sh" sutando-core watcher --socket "$T/x.sock" --skip-if-queued watcher >/dev/null 2>&1) & wait
SEQ="$(grep send-keys "$TMUX_LOG" | sed -E 's/.*-l \/model sonnet$/lit:model/; s/.*-l watcher$/lit:watcher/; s/.*Enter$/enter/' | tr '\n' ' ')"
case "$SEQ" in "lit:model enter lit:watcher enter "|"lit:watcher enter lit:model enter ") ok "18 cross-sender: a switch and an app watcher line serialize through one lock";; *) fail "18" "$SEQ";; esac
settings_untouched && ok "19 after every case above, settings.json is byte-identical to the start" || fail "19 settings" "$(cat "$T/cfg/settings.json")"
# --- the runtime gate fails closed: bogus / empty resolver output refuses before any record
rm -f "$T/state/model-switch.json"; : > "$T/tmux.log"
rc=$(SUTANDO_CORE_RUNTIME=bogus run opus); [ "$rc" = 4 ] && [ ! -e "$T/state/model-switch.json" ] && ! grep -q send-keys "$T/tmux.log" && grep -q "unrecognized\|could not be resolved" "$T/err" \
  && ok "20 unrecognized runtime: exit 4, no record, nothing sent" || fail "20 bogus runtime" "rc=$rc $(cat "$T/err")"
mkdir -p "$T/fakerepo/scripts"; printf '#!/bin/sh\ncase "$1" in core-runtime) exit 1;; *) exec bash "%s/scripts/sutando-config.sh" "$@";; esac\n' "$HERE" > "$T/fakerepo/scripts/sutando-config.sh"; chmod +x "$T/fakerepo/scripts/sutando-config.sh"
ln -sfn "$HERE/skills" "$T/fakerepo/skills"
rc=$(bash "$T/fakerepo/skills/model-switch/scripts/switch-model.sh" opus --state-dir "$T/state" --brain "$T/cfg" > "$T/out" 2> "$T/err"; echo $?)
[ "$rc" = 4 ] && [ ! -e "$T/state/model-switch.json" ] && grep -q "could not be resolved" "$T/err" && ok "21 resolver exits nonzero: exit 4, no record" || fail "21 resolver failure" "rc=$rc $(cat "$T/err")"

echo; [ $fails -eq 0 ] && echo "switch-model: all 21 checks pass" || { echo "switch-model: $fails FAILED"; exit 1; }
