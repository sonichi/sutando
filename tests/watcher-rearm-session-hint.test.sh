#!/bin/bash
# The SessionStart(compact|resume) watcher re-arm hint, run from a bundle whose
# watcher_identity.py is a stub answering $STUB_VERDICT; the policy module is real.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -z "$PY" ]; then echo "FAIL — scripts/sutando-config.sh python-bin resolved nothing"; exit 1; fi

BUNDLE="$(mktemp -d "${TMPDIR:-/tmp}/sutando-rearm-hint.XXXXXX")"
trap 'rm -rf "$BUNDLE"' EXIT
BUNDLE="$(cd "$BUNDLE" && pwd -P)"
mkdir -p "$BUNDLE/src" "$BUNDLE/scripts" "$BUNDLE/workspace/tasks" "$BUNDLE/workspace/state"
for f in src/watcher-rearm-session-hint.sh src/watcher_rearm.py; do
  [ -f "$REPO/$f" ] || { echo "FAIL — $f is missing"; exit 1; }
  cp "$REPO/$f" "$BUNDLE/$f"
done
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' "$BUNDLE" "$PY" > "$BUNDLE/scripts/sutando-config.sh"
ARGV_LOG="$BUNDLE/identity.argv"
cat > "$BUNDLE/src/watcher_identity.py" <<EOF
import os, sys
open("$ARGV_LOG", "a").write(" ".join(sys.argv[1:]) + "\n")
v = os.environ.get("STUB_VERDICT", "unknown")
print(v)
sys.exit(2 if v == "unknown" else 0)
EOF
WS="$BUNDLE/workspace"
HINT="$BUNDLE/src/watcher-rearm-session-hint.sh"
run_hint() {  # $1 verdict, rest: env assignments naming who the session is
  local v="$1"; shift
  (cd / && echo '{"hook_event_name":"SessionStart","source":"compact"}' \
    | env -u SUTANDO_CORE_SESSION -u SUTANDO_INSTANCE_ID STUB_VERDICT="$v" "$@" bash "$HINT")
}
field() { "$PY" -c 'import json,sys; d=json.load(sys.stdin)["hookSpecificOutput"]; print(d[sys.argv[1]])' "$1" 2>/dev/null; }

echo "core session, inbox unwatched after compaction:"
: > "$ARGV_LOG"
OUT="$(run_hint no SUTANDO_CORE_SESSION=1)"
[ "$(printf '%s' "$OUT" | field hookEventName)" = "SessionStart" ] && ok "emits SessionStart hook JSON" || bad "emits SessionStart hook JSON" "got: ${OUT:0:200}"
CTX="$(printf '%s' "$OUT" | field additionalContext)"
case "$CTX" in
  *"via the Monitor tool, command: bash \"$BUNDLE/src/watch-tasks-stream.sh\" --role session --inbox \"$WS/tasks\""*) ok "names the absolute re-arm command for the core inbox" ;;
  *) bad "names the absolute re-arm command for the core inbox" "ctx: ${CTX:0:400}" ;;
esac
case "$CTX" in *"timeout_ms: 1800000"*) ok "names the Monitor timeout" ;; *) bad "names the Monitor timeout" "ctx: ${CTX:0:400}" ;; esac
grep -qx "role-present session --inbox $WS/tasks --ready $WS/state" "$ARGV_LOG" && ok "asks for a READY session-role holder of its own inbox" || bad "asks for a READY session-role holder of its own inbox" "argv: $(cat "$ARGV_LOG")"

echo "nothing is injected unless the inbox is proven unwatched:"
OUT="$(run_hint yes SUTANDO_CORE_SESSION=1)"; [ -z "$OUT" ] && ok "watched inbox: silent" || bad "watched inbox: silent" "got: ${OUT:0:200}"
OUT="$(run_hint unknown SUTANDO_CORE_SESSION=1)"; [ -z "$OUT" ] && ok "unobservable host: silent" || bad "unobservable host: silent" "got: ${OUT:0:200}"
: > "$ARGV_LOG"
OUT="$(run_hint no)"
[ -z "$OUT" ] && [ ! -s "$ARGV_LOG" ] && ok "guest session: silent, and never probes" || bad "guest session: silent, and never probes" "out=${OUT:0:120} argv=$(cat "$ARGV_LOG")"

echo "pool worker re-arms its own delivery folder:"
: > "$ARGV_LOG"
CTX="$(run_hint no SUTANDO_INSTANCE_ID=w1 | field additionalContext)"
case "$CTX" in
  *'command: bash "$SUTANDO_WATCHER_CMD" "$SUTANDO_TASKS_DIR" --role session --inbox "$SUTANDO_TASKS_DIR"'*) ok "worker re-arm command uses its launcher's env" ;;
  *) bad "worker re-arm command uses its launcher's env" "ctx: ${CTX:0:400}" ;;
esac
grep -q -- "--inbox $WS/deliveries/w1 " "$ARGV_LOG" && ok "worker asks about deliveries/w1" || bad "worker asks about deliveries/w1" "argv: $(cat "$ARGV_LOG")"

echo "the Stop hook and the hint share one target:"
T="$(cd "$BUNDLE" && env -u SUTANDO_INSTANCE_ID "$PY" src/watcher_rearm.py target --repo "$BUNDLE" --workspace "$WS")"
[ "$(printf '%s\n' "$T" | sed -n 1p)" = "$WS/tasks" ] && ok "target line 1 is the core inbox" || bad "target line 1 is the core inbox" "got: $T"
grep -q 'watcher_rearm.py" target' "$REPO/src/check-pending-tasks.sh" && ok "check-pending-tasks.sh reads its inbox/command from watcher_rearm.py" || bad "check-pending-tasks.sh reads its inbox/command from watcher_rearm.py" "no call found"

echo "installer registers it under SessionStart compact|resume, once:"
IREPO="$BUNDLE/irepo"
mkdir -p "$IREPO/scripts" "$IREPO/src"
cp "$REPO/scripts/install-watcher-rearm-hook.sh" "$REPO/scripts/python-binary.sh" "$IREPO/scripts/"
cp "$REPO/src/claude_hooks_settings.py" "$REPO/src/watcher-rearm-session-hint.sh" "$IREPO/src/"
for _ in 1 2; do (env -u SUTANDO_CLAUDE_WORKING_DIR bash "$IREPO/scripts/install-watcher-rearm-hook.sh" >/dev/null 2>"$BUNDLE/ierr") || bad "installer exits 0" "$(cat "$BUNDLE/ierr")"; done
N="$("$PY" -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for e in d["hooks"].get("SessionStart",[]) if e.get("matcher")=="compact|resume"
          for h in e["hooks"] if "watcher-rearm-session-hint.sh" in h["command"]))' "$IREPO/.claude/settings.json" 2>&1)"
[ "$N" = "1" ] && ok "exactly one compact|resume entry after two runs" || bad "exactly one compact|resume entry after two runs" "got: $N"

echo "the Claude launch chokepoint runs the installer:"
grep -q 'bash "$REPO/scripts/install-watcher-rearm-hook.sh"' "$REPO/src/agent/claude/cli/session-launch.sh" && ok "session-launch.sh calls it" || bad "session-launch.sh calls it" "no call found"

[ "$FAILED" = 0 ] && echo "PASS" || { echo "FAIL"; exit 1; }
