#!/usr/bin/env bash
# checkWatcher's recovery no longer types a keystroke into a live pane (#4451
# redirect): it shells out to the canonical launcher dispatcher, pinned to the
# runtime the LIVE session already confirmed. This pins that the runtime gate
# still runs BEFORE the probe (Claude-only, same invariant as before), that
# recovery names the dispatcher (not the Claude-specific launcher directly, and
# not tmux-send-line), and that the now-unnecessary busy-CLI gate is gone.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; F="$HERE/src/Sutando/main.swift"; C="$HERE/src/Sutando/SutandoConfig.swift"
fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }

# --- SutandoConfig resolver cluster, unchanged shape --------------------------
grep -q 'static func resolveCoreRuntime' "$C" && grep -q 'SUTANDO_CORE_RUNTIME' "$C" \
  && grep -q 'loadConfig(repoRoot: explicitRoot)' "$C" \
  && ok "1 runtime resolves from the shared config loader + env override" || fail "1" "resolver missing or bypasses loadConfig"
grep -q 'show-environment' "$C" && grep -q 'static func sessionCoreRuntime' "$C" \
  && grep -q 'SutandoConfig.sessionCoreRuntime(socket:' "$F" \
  && ok "2 session runtime resolves in SutandoConfig; the app is a thin caller" || fail "2" "session read missing or duplicated in the app"
grep -q 'supportedCoreRuntimes: Set<String> = \["claude", "codex"\]' "$C" \
  && ok "3 supported set matches sutando_config.py" || fail "3" "supported runtimes drifted"

# --- checkWatcher's body, isolated -------------------------------------------
BODY="$(awk '/^    func checkWatcher\(\) \{/,0' "$F" | awk '/^    func /{n++} n<=1')"
[ -n "$BODY" ] || fail "0" "checkWatcher() not found"

ck_body(){ printf '%s' "$BODY" | grep -q -- "$1"; }

ck_body 'sessionCoreRuntime()' \
  && ok "4 checkWatcher consults the core runtime" || fail "4" "no runtime gate in checkWatcher"
GATE_LINE=$(printf '%s\n' "$BODY" | grep -n 'sessionCoreRuntime()' | head -1 | cut -d: -f1)
PGREP_LINE=$(printf '%s\n' "$BODY" | grep -n 'watcherProcessSeen()' | head -1 | cut -d: -f1)
[ -n "$GATE_LINE" ] && [ -n "$PGREP_LINE" ] && [ "$GATE_LINE" -lt "$PGREP_LINE" ] \
  && ok "5 ...and returns BEFORE probing for the Claude-only watcher" \
  || fail "5" "gate at ${GATE_LINE:-none} is not before the probe at ${PGREP_LINE:-none}"
ck_body 'rt != "claude"' \
  && ok "6 ...skipping only a positively-identified non-Claude runtime" || fail "6" "gate does not key on a known runtime"

# --- the new recovery: dispatcher, not a keystroke ---------------------------
ck_body 'src/agent/start-cli\.sh' \
  && ok "7 recovery names the launcher DISPATCHER (not the claude-specific launcher directly)" \
  || fail "7" "does not call src/agent/start-cli.sh"
if ck_body 'src/agent/claude/cli/start-cli\.sh'; then
  fail "7b" "calls the Claude-specific launcher directly, bypassing the dispatcher's own runtime routing"
else
  ok "7b ...and does NOT hardcode the Claude-specific launcher path"
fi
ck_body '"--runtime", "claude"' \
  && ok "8 pins --runtime claude, using the session runtime just confirmed" || fail "8" "does not pin --runtime"
if ck_body 'line: "watcher"'; then
  fail "9" "still sends the 'watcher' keystroke somewhere in checkWatcher"
else
  ok "9 no 'watcher' keystroke anywhere in checkWatcher"
fi
if ck_body 'tmuxSendLine'; then
  fail "10" "still calls tmuxSendLine — the pane-injection path this redirect removes"
else
  ok "10 tmuxSendLine is not called from checkWatcher"
fi

# --- the busy-CLI gate is gone: the new recovery never touches the pane ------
if ck_body 'cliIsWorking()'; then
  fail "11" "still gates on cliIsWorking(), no longer needed since start-cli.sh never writes into the pane"
else
  ok "11 no cliIsWorking() gate — the dispatcher call is safe to fire unconditionally"
fi
grep -q 'func cliIsWorking' "$F" \
  && fail "12" "cliIsWorking() is still defined — dead code under the new design" \
  || ok "12 cliIsWorking() is gone (it has no remaining caller)"
grep -q 'func processAgeSeconds' "$F" \
  && fail "13" "processAgeSeconds() is still defined — dead code, cliIsWorking's only caller" \
  || ok "13 processAgeSeconds() is gone (it has no remaining caller)"

echo; [ $fails -eq 0 ] && echo "app-watcher-recovery-runtime: all checks pass" || { echo "app-watcher-recovery-runtime: $fails FAILED"; exit 1; }
