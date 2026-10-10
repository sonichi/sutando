#!/bin/bash
# src/install-claude-hooks.sh now only sweeps: it removes the entries earlier
# installers wrote (project file + core config dir) and writes no hook anywhere.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
PASS=0; FAIL=0
ok() { if [ "$2" = 0 ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $1${3:+ — $3}"; fi; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks sweep.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
REPO="$ROOT/repo with 'quote"
WS="$ROOT/ws"
mkdir -p "$REPO/src" "$REPO/scripts" "$REPO/.claude" "$WS/.claude-sutando" "$ROOT/home"
REPO="$(cd "$REPO" && pwd)"; REAL_REPO="$(cd "$REPO" && pwd -P)"
for f in src/install-claude-hooks.sh src/claude_hooks_settings.py src/skill_hooks.py src/sutando_config.py \
         scripts/sutando-config.sh scripts/python-binary.sh; do cp "$REAL/$f" "$REPO/$f"; done
for s in check-pending-tasks.sh session-handoff.sh turn-start.sh; do printf '#!/bin/bash\necho %s-RAN\n' "$s" > "$REPO/src/$s"; done
mkdir -p "$REPO/skills/demo/hooks"
printf '#!/usr/bin/env python3\n' > "$REPO/skills/demo/hooks/g.py"
echo '{"name":"demo","hooks":[{"event":"PreToolUse","command":"./hooks/g.py"}]}' > "$REPO/skills/demo/manifest.json"

# Hermetic: the test-mode workspace keeps the config-dir resolver inside $ROOT.
run_sweep() {
  env -u CLAUDE_CONFIG_DIR -u SUTANDO_CLAUDE_WORKING_DIR HOME="$ROOT/home" \
    SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" bash "$REPO/src/install-claude-hooks.sh" "$@"
}

# The previous installer's own quoting, to build its output byte-for-byte.
shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
SKILL_Q="$(python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$(cd "$REPO/skills/demo/hooks" && pwd -P)/g.py")"

OPER="$ROOT/operator/\$CUSTOM_ROOT/src"
mkdir -p "$OPER"; printf '#!/bin/bash\necho OPERATOR-RAN\n' > "$OPER/session-handoff.sh"
ESCAPED_OPERATOR="bash \"$ROOT/operator/\\\$CUSTOM_ROOT/src/session-handoff.sh\" \"\$TRANSCRIPT_PATH\""
H="bash $(shq "$REPO/src/session-handoff.sh") \"\$TRANSCRIPT_PATH\""
export S_PRECOMPACT="$H"$'\n'"bash $(shq "$REPO/src/archive-transcript.sh") \"\$HOME/Desktop/sutando-conversations/\""$'\n'"$ESCAPED_OPERATOR"
export S_SESSIONEND="$H"
# Resolved-path form: an installer run through a symlinked path wrote this one.
export S_STOP="bash $(shq "$REAL_REPO/src/check-pending-tasks.sh")"$'\n'"echo operator-stop"
export S_UPS="bash $(shq "$REPO/src/turn-start.sh")"
export S_SS="bash \"$REPO/src/schedule-crons-session-hint.sh\""$'\n'"bash \"$REPO/src/personal-claude-compact-hint.sh\""$'\n'"bash \"$REPO/src/watcher-rearm-session-hint.sh\""
export S_PTU="[ -f $SKILL_Q ] || exit 0; exec python3 $SKILL_Q"$'\n'"bash -x $(shq "$REPO/src/turn-start.sh")"
export S_CORE="bash \"$REPO/src/session-handoff.sh\" \"\${TRANSCRIPT_PATH:-}\""
python3 - "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json" <<'PY'
import json, os, sys
def g(cmds, m=""): return {"matcher": m, "hooks": [{"type": "command", "command": c} for c in cmds]}
E = lambda k: os.environ[k].split("\n")
ss = E("S_SS")
project = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {
    "PreCompact": [g(E("S_PRECOMPACT"))], "SessionEnd": [g(E("S_SESSIONEND"))],
    "Stop": [g(E("S_STOP"))], "UserPromptSubmit": [g(E("S_UPS"))],
    "SessionStart": [g([ss[0]]), g([ss[1]], "compact"), g([ss[2]], "compact|resume")],
    "PreToolUse": [g(E("S_PTU"))]}}
core = {"theme": "dark", "hooks": {"SessionEnd": [g(E("S_CORE"))]}}
json.dump(project, open(sys.argv[1], "w"), indent=2)
json.dump(core, open(sys.argv[2], "w"), indent=2)
PY
cmds() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("\n".join(h["command"] for gs in d.get("hooks",{}).values() for g in gs for h in g["hooks"]))' "$1"; }

# 1. --dry-run names what it would remove and writes nothing.
before="$(cat "$REPO/.claude/settings.json")"
out="$(run_sweep --dry-run 2>&1)"; rc=$?
ok "dry run exits 0" "$rc" "$out"
ok "dry run counts every installer-written entry (9 project + 1 config dir)" \
   "$(echo "$out" | grep -q '10 owned entries found' && echo 0 || echo 1)" "$out"
ok "dry run leaves the project file byte-identical" "$([ "$(cat "$REPO/.claude/settings.json")" = "$before" ] && echo 0 || echo 1)"

# 2. The real run removes exactly those and keeps the operator's own entries.
out="$(run_sweep 2>&1)"; rc=$?
ok "sweep exits 0" "$rc" "$out"
ok "sweep reports 10 removed" "$(echo "$out" | grep -q '10 owned entries removed' && echo 0 || echo 1)" "$out"
left="$(cmds "$REPO/.claude/settings.json")"
want="$ESCAPED_OPERATOR"$'\n'"echo operator-stop"$'\n'"bash -x $(shq "$REPO/src/turn-start.sh")"
ok "exactly the three operator entries are left in the project file" \
   "$([ "$(echo "$left" | sort)" = "$(echo "$want" | sort)" ] && echo 0 || echo 1)" "$left"
ok "an operator Stop hook survives" "$(echo "$left" | grep -qx 'echo operator-stop' && echo 0 || echo 1)"
ok "an operator 'bash -x' wrapper around our script survives" "$(echo "$left" | grep -q '^bash -x ' && echo 0 || echo 1)"
ok "an escaped-literal operator hook naming a real script survives" \
   "$(echo "$left" | grep -qxF "$ESCAPED_OPERATOR" && echo 0 || echo 1)" "$left"
TRANSCRIPT_PATH=/dev/null; export TRANSCRIPT_PATH
ok "and that surviving hook still runs the operator's script" \
   "$([ "$(bash -c "$ESCAPED_OPERATOR")" = OPERATOR-RAN ] && echo 0 || echo 1)"
ok "non-hook keys are untouched" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["permissions"]=={"allow":["Bash(ls)"]} else 1)' "$REPO/.claude/settings.json"; echo $?)"
ok "the config-dir copy of session-handoff is removed" \
   "$([ -z "$(cmds "$WS/.claude-sutando/settings.json")" ] && echo 0 || echo 1)"
ok "config-dir non-hook keys are untouched" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["theme"]=="dark" else 1)' "$WS/.claude-sutando/settings.json"; echo $?)"

# 3. Idempotent: a second run changes nothing.
snap="$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")"
out="$(run_sweep 2>&1)"
ok "second run removes nothing" "$(echo "$out" | grep -q '0 owned entries removed' && echo 0 || echo 1)" "$out"
ok "second run leaves both files byte-identical" \
   "$([ "$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")" = "$snap" ] && echo 0 || echo 1)"

# 4. It never creates a settings file or a .claude dir where none existed.
rm -rf "$REPO/.claude" "$WS/.claude-sutando"
run_sweep >/dev/null 2>&1
ok "no project .claude/ is created" "$([ ! -e "$REPO/.claude" ] && echo 0 || echo 1)"
ok "no config-dir settings.json is created" "$([ ! -e "$WS/.claude-sutando/settings.json" ] && echo 0 || echo 1)"

# 5. An unreadable file is reported, left as-is, and fails the run.
mkdir -p "$REPO/.claude"; printf '{broken' > "$REPO/.claude/settings.json"
out="$(run_sweep 2>&1)"; rc=$?
ok "a malformed project file fails the run" "$([ "$rc" = 1 ] && echo 0 || echo 1)" "rc=$rc $out"
ok "and is left byte-identical" "$([ "$(cat "$REPO/.claude/settings.json")" = '{broken' ] && echo 0 || echo 1)"

# 6. No usable python: say so and exit 2, writing nothing.
NOPY="$ROOT/nopy"; mkdir -p "$NOPY/src" "$NOPY/scripts"
cp "$REAL/src/install-claude-hooks.sh" "$NOPY/src/"
printf 'resolve_python() { :; }\n' > "$NOPY/scripts/python-binary.sh"
out="$(bash "$NOPY/src/install-claude-hooks.sh" 2>&1)"; rc=$?
ok "no python exits 2 with a reason" "$([ "$rc" = 2 ] && echo "$out" | grep -q 'no runnable python3' && echo 0 || echo 1)" "rc=$rc $out"

echo "install-claude-hooks: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
