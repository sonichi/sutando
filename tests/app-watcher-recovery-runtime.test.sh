#!/usr/bin/env bash
# The menu-bar app's 300s watcher-recovery send must select the pane parser for the
# session's REAL runtime and refuse a pane carrying unsent text. Without --runtime the
# sender parses every pane as Claude, so a Codex composer's `›` line reads as empty and
# the watchdog appends `watcher` to an operator's draft and presses Enter.
# Two halves: source pins for wiring CI cannot compile, then the argv the app emits
# driven through the REAL sender against a tmux shim.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; F="$HERE/src/Sutando/main.swift"; C="$HERE/src/Sutando/SutandoConfig.swift"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT; fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }

# --- wiring pins -----------------------------------------------------------
CALL="$(grep -A3 'line: "watcher"' "$F")"
printf '%s' "$CALL" | grep -q 'runtime: sessionCoreRuntime()' \
  && ok "1 recovery send resolves the SESSION runtime" || fail "1" "watcher call does not use sessionCoreRuntime()"
printf '%s' "$CALL" | grep -q 'refuseIfPending: true' \
  && ok "2 ...and refuses a pane with unsent text" || fail "2" "refuseIfPending not passed"
printf '%s' "$CALL" | grep -q 'skipIfQueued: "watcher"' \
  && ok "3 ...while retaining the queued-word skip" || fail "3" "skipIfQueued dropped"
grep -q 'if let r = runtime { args += \["--runtime", r\] }' "$F" \
  && grep -q 'if refuseIfPending { args += \["--refuse-if-pending"\] }' "$F" \
  && ok "4 the sender wrapper forwards both flags" || fail "4" "wrapper does not forward the flags"
# One resolver, not a second detection: the Swift side reads the same merged config.
grep -q 'static func resolveCoreRuntime' "$C" && grep -q 'SUTANDO_CORE_RUNTIME' "$C" \
  && grep -q 'loadConfig(repoRoot: explicitRoot)' "$C" \
  && ok "5 runtime resolves from the shared config loader + env override" || fail "5" "resolver missing or bypasses loadConfig"
# The SESSION outranks config: start-cli.sh exports the runtime into the session,
# so a one-command Codex trial leaves config saying claude.
grep -q 'show-environment' "$C" && grep -q 'static func sessionCoreRuntime' "$C" \
  && grep -q 'SutandoConfig.sessionCoreRuntime(socket:' "$F" \
  && ok "5b session runtime resolves in SutandoConfig; the app is a thin caller" || fail "5b" "session read missing or duplicated in the app"
grep -q 'supportedCoreRuntimes: Set<String> = \["claude", "codex"\]' "$C" \
  && ok "6 supported set matches sutando_config.py" || fail "6" "supported runtimes drifted"

# --- behaviour through the real sender -------------------------------------
mkdir -p "$T/bin"; cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in
  *" has-session "*) ;;
  *" capture-pane "*)
    n=$(( $(cat "$TMUX_LOG.n" 2>/dev/null || echo 0) + 1 )); printf %s "$n" > "$TMUX_LOG.n"
    if [ "$n" -ge 2 ] && [ -n "${TMUX_PANE_TEXT_AFTER+x}" ]; then printf '%b' "$TMUX_PANE_TEXT_AFTER"
    else printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}"; fi ;;
esac
exit 0
SH
chmod +x "$T/bin/tmux"; export TMUX_LOG="$T/log"
run(){ : > "$TMUX_LOG"; rm -f "$TMUX_LOG.n"; PATH="$T/bin:$PATH" bash "$HERE/scripts/tmux-send-line.sh" "$@" >/dev/null 2>&1; echo $?; }
sent(){ grep -q -- "send-keys -t sutando-core -l watcher" "$TMUX_LOG"; }
# argv the app emits on a Codex core
A=(sutando-core watcher --socket "$T/s.sock" --skip-if-queued watcher --runtime codex --refuse-if-pending)

rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m half typed\n' run "${A[@]}")
[ "$rc" = 5 ] && ! sent && ok "7 codex pane with an operator draft → 5, nothing sent" || fail "7" "rc=$rc sent=$(sent && echo yes || echo no)"
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m watcher\n' run "${A[@]}")
[ "$rc" = 6 ] && ! sent && ok "8 codex pane with 'watcher' already queued → 6, no duplicate" || fail "8" "rc=$rc"
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m \033[2mImprove documentation in @filename\033[0m\n' TMUX_PANE_TEXT_AFTER='› watcher\n' run "${A[@]}")
[ "$rc" = 0 ] && sent && ok "9 codex empty composer → recovery still sends" || fail "9" "rc=$rc — recovery regressed"
rc=$(TMUX_PANE_TEXT='────\n❯ \n────\n' TMUX_PANE_TEXT_AFTER='❯ watcher\n' run sutando-core watcher --socket "$T/s.sock" --skip-if-queued watcher --runtime claude --refuse-if-pending)
[ "$rc" = 0 ] && sent && ok "10 claude clear prompt → unchanged, still sends" || fail "10" "rc=$rc"
# The defect itself: the pre-fix argv (no --runtime/--refuse) overwrites a Codex draft.
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m half typed\n' TMUX_PANE_TEXT_AFTER='› watcher\n' run sutando-core watcher --socket "$T/s.sock" --skip-if-queued watcher)
[ "$rc" = 0 ] && sent && ok "11 control: the pre-fix argv DOES overwrite the draft (defect is real)" || fail "11" "control did not reproduce; rc=$rc"

# --- the round-2 defect: config and session disagree ------------------------
# `SUTANDO_CORE_RUNTIME=codex bash src/agent/start-cli.sh --restart` (docs/codex-core.md)
# leaves config at claude. Resolving from config emits --runtime claude and submits the draft.
mkdir -p "$T/sess"; cat > "$T/sess/tmux" <<'SH'
#!/usr/bin/env bash
case " $* " in
  *" show-environment "*) printf 'SUTANDO_CORE_RUNTIME=%s\n' "${SESSION_RUNTIME:--SUTANDO_CORE_RUNTIME}" ;;
esac
exit 0
SH
chmod +x "$T/sess/tmux"
SESSION_RUNTIME=codex PATH="$T/sess:$PATH" tmux -S x show-environment -t =sutando-core SUTANDO_CORE_RUNTIME \
  | grep -q 'SUTANDO_CORE_RUNTIME=codex' \
  && ok "15 shim: a codex session answers show-environment with codex" || fail "15" "shim wrong"
# The behaviour that matters: with the session saying codex, the argv the app now emits
# refuses the draft. Pinned through the REAL sender, not a source grep.
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m half typed\n' run sutando-core watcher --socket "$T/s.sock" --skip-if-queued watcher --runtime codex --refuse-if-pending)
[ "$rc" = 5 ] && ! sent \
  && ok "16 session=codex + config=claude → draft refused, nothing sent" || fail "16" "rc=$rc sent=$(sent && echo yes || echo no)"
# An unresolved runtime must NOT fall through to the Claude parser.
grep -A3 'guard let rt = sessionCoreRuntime() else {' "$F" | grep -q 'return' \
  && ok "17 unresolved runtime skips the watchdog instead of defaulting to Claude" || fail "17" "unresolved still continues"

# --- the watchdog does not run at all on a non-Claude core ------------------
# Both halves are Claude-only: the probe looks for watch-tasks-stream.sh and the
# remedy is a word only the Claude CLI parses as a restart prompt. On a Codex core
# the probe can only ever report "dead", so before this gate the app typed `watcher`
# into an idle Codex pane every 300s, forever.
BODY="$(awk '/^    func checkWatcher\(\) \{/,/^    func cliIsWorking/' "$F")"
printf '%s' "$BODY" | grep -q 'resolveCoreRuntime' \
  && ok "12 checkWatcher consults the core runtime" || fail "12" "no runtime gate in checkWatcher"
GATE_LINE=$(printf '%s\n' "$BODY" | grep -n 'sessionCoreRuntime()' | head -1 | cut -d: -f1)
PGREP_LINE=$(printf '%s\n' "$BODY" | grep -n 'watcherProcessSeen()' | head -1 | cut -d: -f1)
[ -n "$GATE_LINE" ] && [ -n "$PGREP_LINE" ] && [ "$GATE_LINE" -lt "$PGREP_LINE" ] \
  && ok "13 ...and returns BEFORE probing for the Claude-only watcher" \
  || fail "13" "gate at ${GATE_LINE:-none} is not before the probe at ${PGREP_LINE:-none}"
printf '%s' "$BODY" | grep -qE 'rt != "claude"' \
  && ok "14 ...skipping only a positively-identified non-Claude runtime" || fail "14" "gate does not key on a known runtime"

echo; [ $fails -eq 0 ] && echo "app-watcher-recovery-runtime: all checks pass" || { echo "app-watcher-recovery-runtime: $fails FAILED"; exit 1; }
