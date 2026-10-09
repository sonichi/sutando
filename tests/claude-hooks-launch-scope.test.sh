#!/bin/bash
# The launcher's --settings JSON carries every owned and skill hook; after a launch no settings
# file a guest session reads (project file, core config dir) still holds one.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
PASS=0; FAIL=0
ok() { if [ "$2" = 0 ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $1${3:+ — $3}"; fi; }
command -v node >/dev/null 2>&1 || { echo "SKIP: node is required to build the launch settings"; exit 0; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando launch scope.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
REPO="$ROOT/repo"; WS="$ROOT/ws"
mkdir -p "$REPO" "$WS/.claude-sutando" "$ROOT/home"
REPO="$(cd "$REPO" && pwd)"
for f in src/agent/claude/cli/session-launch.sh src/agent/claude/cli/build-core-settings.mjs \
         src/install-claude-hooks.sh src/claude_hooks_settings.py src/skill_hooks.py src/sutando_config.py \
         scripts/python-binary.sh; do mkdir -p "$(dirname "$REPO/$f")"; cp "$REAL/$f" "$REPO/$f"; done
OWNED="check-pending-tasks.sh turn-start.sh session-handoff.sh schedule-crons-session-hint.sh personal-claude-compact-hint.sh watcher-rearm-session-hint.sh"
for s in $OWNED; do printf '#!/bin/bash\n' > "$REPO/src/$s"; done
mkdir -p "$REPO/hooks" "$REPO/skills/demo/hooks"
: > "$REPO/hooks/skip-ask-user-question.py"
: > "$REPO/skills/demo/hooks/g.py"
echo '{"name":"demo","hooks":[{"event":"Stop","command":"./hooks/g.py"}]}' > "$REPO/skills/demo/manifest.json"

# What a pre-change host carries: the old installers' output in the project file
# and the migration's catchup hook in the core config dir.
export R="$REPO"
python3 - "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json" <<'PY'
import json, os, sys
r = os.environ["R"]
q = lambda p: "'" + p.replace("'", "'\\''") + "'"
g = lambda *c, m="": {"matcher": m, "hooks": [{"type": "command", "command": x} for x in c]}
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
json.dump({"hooks": {
    "Stop": [g(f"bash {q(r + '/src/check-pending-tasks.sh')}", "echo operator")],
    "UserPromptSubmit": [g(f"bash {q(r + '/src/turn-start.sh')}")],
    "PreCompact": [g(f'bash {q(r + "/src/session-handoff.sh")} "$TRANSCRIPT_PATH"')],
    "SessionEnd": [g(f'bash {q(r + "/src/session-handoff.sh")} "$TRANSCRIPT_PATH"')],
    "SessionStart": [g(f'bash "{r}/src/schedule-crons-session-hint.sh"'),
                     g(f'bash "{r}/src/personal-claude-compact-hint.sh"', m="compact"),
                     g(f'bash "{r}/src/watcher-rearm-session-hint.sh"', m="compact|resume")],
}}, open(sys.argv[1], "w"))
json.dump({"hooks": {"SessionEnd": [g(f'bash "{r}/src/session-handoff.sh" "${{TRANSCRIPT_PATH:-}}"')]}},
          open(sys.argv[2], "w"))
PY

# The same launcher steps start-cli.sh and launch-worker-session.sh run.
OUT="$ROOT/launch.out"
# CLAUDE_CONFIG_DIR as resolve_claude_config_dir_and_seed exports it before this step.
env -u SUTANDO_CLAUDE_WORKING_DIR -u SUTANDO_OBS_ENDPOINT HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$WS/.claude-sutando" \
  SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" REPO="$REPO" bash -c '
  . "$REPO/src/agent/claude/cli/session-launch.sh"
  resolve_claude_py
  sweep_project_claude_hooks >/dev/null
  resolve_claude_settings_args >/dev/null
  [ "${SETTINGS_ARGS[0]:-}" = --settings ] && printf "%s" "${SETTINGS_ARGS[1]}"' > "$OUT" 2>"$ROOT/launch.err"
ok "the launcher produced a --settings payload" "$([ -s "$OUT" ] && echo 0 || echo 1)" "$(cat "$ROOT/launch.err")"

commands_of() { python3 -c 'import json,sys
d = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
print("\n".join(h["command"] for gs in (d.get("hooks") or {}).values() for g in gs for h in g["hooks"]))' "$@"; }
LAUNCH="$(commands_of < "$OUT")"
for s in $OWNED; do
  ok "launch settings register $s" "$(echo "$LAUNCH" | grep -q "/src/$s" && echo 0 || echo 1)"
done
SKILL_CMD="$(python3 "$REPO/src/skill_hooks.py" "$REPO" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["command"])')"
ok "launch settings register the skill hook" "$(echo "$LAUNCH" | grep -qxF "$SKILL_CMD" && echo 0 || echo 1)"
ok "launch settings keep transcripts (cleanupPeriodDays)" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["cleanupPeriodDays"] >= 3650 else 1)' "$OUT"; echo $?)"

ok "the config dir carries the same retention for sessions that inherit only CLAUDE_CONFIG_DIR" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("cleanupPeriodDays") == 3650 else 1)' "$WS/.claude-sutando/settings.json"; echo $?)"

# A guest session (no --settings) reads only these two files from Sutando's side.
GUEST="$(commands_of "$REPO/.claude/settings.json"; commands_of "$WS/.claude-sutando/settings.json")"
ok "a guest session sees only the operator's own hook" "$([ "$GUEST" = "echo operator" ] && echo 0 || echo 1)" "$GUEST"
for s in $OWNED; do
  ok "a guest session does not run $s" "$(echo "$GUEST" | grep -q "/src/$s" && echo 1 || echo 0)"
done
ok "a guest session does not run the skill hook" "$(echo "$GUEST" | grep -qF "$SKILL_CMD" && echo 1 || echo 0)"

# A second launch writes nothing back into either file.
snap="$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")"
env -u SUTANDO_CLAUDE_WORKING_DIR HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$WS/.claude-sutando" SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" \
  REPO="$REPO" bash -c '. "$REPO/src/agent/claude/cli/session-launch.sh"; resolve_claude_py
  sweep_project_claude_hooks; resolve_claude_settings_args' >/dev/null 2>&1
ok "a relaunch leaves both settings files byte-identical" \
   "$([ "$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")" = "$snap" ] && echo 0 || echo 1)"

# An operator's own retention is kept, and a file the seed cannot parse is left alone.
seed_once() {
  env -u SUTANDO_OBS_ENDPOINT HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$1" REPO="$REPO" bash -c '
    . "$REPO/src/agent/claude/cli/session-launch.sh"; resolve_claude_py; resolve_claude_settings_args' >/dev/null 2>&1
}
mkdir -p "$ROOT/ccd-own" "$ROOT/ccd-bad"
echo '{"cleanupPeriodDays": 45}' > "$ROOT/ccd-own/settings.json"
printf '{broken' > "$ROOT/ccd-bad/settings.json"
seed_once "$ROOT/ccd-own"; seed_once "$ROOT/ccd-bad"
ok "an operator-set cleanupPeriodDays is kept" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["cleanupPeriodDays"] == 45 else 1)' "$ROOT/ccd-own/settings.json"; echo $?)"
ok "an unparseable config-dir settings file is left byte-identical" \
   "$([ "$(cat "$ROOT/ccd-bad/settings.json")" = '{broken' ] && echo 0 || echo 1)"

echo "claude-hooks-launch-scope: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
