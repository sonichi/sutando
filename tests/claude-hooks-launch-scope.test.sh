#!/bin/bash
# The launcher's --settings JSON carries every owned and skill hook; after a core launch no
# settings file a guest session reads still holds one; a launch without settings keeps them.
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
         src/agent/claude/cli/owned-hooks.json \
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

# A launch whose settings cannot be built (node absent) leaves the copies the core still runs.
LEGACY="$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")"
NONODE="$ROOT/nonode-bin"; mkdir -p "$NONODE"
for b in bash python3 cat dirname mkdir; do ln -s "$(command -v "$b")" "$NONODE/$b"; done
env -u SUTANDO_CLAUDE_WORKING_DIR PATH="$NONODE" HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$WS/.claude-sutando" \
  SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" REPO="$REPO" "$NONODE/bash" -c '
  . "$REPO/src/agent/claude/cli/session-launch.sh"; resolve_claude_py
  resolve_claude_settings_args; sweep_legacy_claude_hooks_for_launch
  echo "settings_count=${#SETTINGS_ARGS[@]}"' > "$ROOT/nonode.out" 2>&1
ok "node absent: no launch settings" "$(grep -q '^settings_count=0$' "$ROOT/nonode.out" && echo 0 || echo 1)" "$(cat "$ROOT/nonode.out")"
ok "node absent: the legacy copies stay byte-identical" \
   "$([ "$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")" = "$LEGACY" ] && echo 0 || echo 1)"

# A builder that fails under the launcher's `set -e` neither aborts the launch nor loses the copies.
cp -R "$REPO" "$ROOT/broken"; rm "$ROOT/broken/src/agent/claude/cli/owned-hooks.json"
env -u SUTANDO_CLAUDE_WORKING_DIR HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$WS/.claude-sutando" \
  SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" REPO="$ROOT/broken" bash -ec '
  . "$REPO/src/agent/claude/cli/session-launch.sh"; resolve_claude_py
  resolve_claude_settings_args; sweep_legacy_claude_hooks_for_launch
  echo "settings_count=${#SETTINGS_ARGS[@]}"' > "$ROOT/broken.out" 2>&1
ok "a failed settings build: the launch goes on without settings" \
   "$(grep -q '^settings_count=0$' "$ROOT/broken.out" && echo 0 || echo 1)" "$(tail -3 "$ROOT/broken.out")"
ok "a failed settings build: the legacy copies stay byte-identical" \
   "$([ "$(cat "$ROOT/broken/.claude/settings.json")" = "$(cat "$REPO/.claude/settings.json")" ] && echo 0 || echo 1)"

# The steps start-cli.sh runs for a core launch once no core is live.
OUT="$ROOT/launch.out"
# CLAUDE_CONFIG_DIR as resolve_claude_config_dir_and_seed exports it before this step.
env -u SUTANDO_CLAUDE_WORKING_DIR -u SUTANDO_OBS_ENDPOINT HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$WS/.claude-sutando" \
  SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" REPO="$REPO" bash -c '
  . "$REPO/src/agent/claude/cli/session-launch.sh"
  resolve_claude_py
  resolve_claude_settings_args >/dev/null 2>&1
  sweep_legacy_claude_hooks_for_launch >/dev/null
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
  resolve_claude_settings_args; sweep_legacy_claude_hooks_for_launch' >/dev/null 2>&1
ok "a relaunch leaves both settings files byte-identical" \
   "$([ "$(cat "$REPO/.claude/settings.json" "$WS/.claude-sutando/settings.json")" = "$snap" ] && echo 0 || echo 1)"

# Retention: --settings outranks the user and project files, so an operator's value there must
# still be the effective one. Effective = first source that sets it, in Claude Code's order.
launch_payload() {
  env -u SUTANDO_OBS_ENDPOINT -u SUTANDO_CLAUDE_WORKING_DIR HOME="$ROOT/home" CLAUDE_CONFIG_DIR="$1" REPO="$REPO" bash -c '
    . "$REPO/src/agent/claude/cli/session-launch.sh"; resolve_claude_py; resolve_claude_settings_args >/dev/null 2>&1
    printf "%s" "${SETTINGS_ARGS[1]:-}"'
}
effective_days() {
  python3 - "$@" <<'PY'
import json, sys
for src in sys.argv[1:]:
    try:
        d = json.loads(src) if src.startswith("{") else json.load(open(src))
    except Exception:
        continue
    if isinstance(d, dict) and "cleanupPeriodDays" in d:
        print(d["cleanupPeriodDays"]); break
else:
    print(30)
PY
}
mkdir -p "$ROOT/ccd-own" "$ROOT/ccd-bad" "$ROOT/ccd-new" "$ROOT/ccd-0600"
echo '{"cleanupPeriodDays": 45}' > "$ROOT/ccd-own/settings.json"
printf '{broken' > "$ROOT/ccd-bad/settings.json"
P="$(launch_payload "$ROOT/ccd-own")"
ok "an operator-set cleanupPeriodDays is kept in the file" \
   "$(python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["cleanupPeriodDays"] == 45 else 1)' "$ROOT/ccd-own/settings.json"; echo $?)"
ok "an operator-set cleanupPeriodDays is the effective one" \
   "$([ "$(effective_days "$P" "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json" "$ROOT/ccd-own/settings.json")" = 45 ] && echo 0 || echo 1)" \
   "$(effective_days "$P" "$ROOT/ccd-own/settings.json")"
python3 - "$REPO/.claude/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["cleanupPeriodDays"] = 60; json.dump(d, open(sys.argv[1], "w"))
PY
P="$(launch_payload "$ROOT/ccd-new")"
ok "a project-set cleanupPeriodDays is the effective one" \
   "$([ "$(effective_days "$P" "$REPO/.claude/settings.json" "$ROOT/ccd-new/settings.json")" = 60 ] && echo 0 || echo 1)"
python3 - "$REPO/.claude/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d.pop("cleanupPeriodDays"); json.dump(d, open(sys.argv[1], "w"))
PY
rm -rf "$ROOT/ccd-new"; mkdir -p "$ROOT/ccd-new"
launch_payload "$ROOT/ccd-bad" >/dev/null
ok "an unparseable config-dir settings file is left byte-identical" \
   "$([ "$(cat "$ROOT/ccd-bad/settings.json")" = '{broken' ] && echo 0 || echo 1)"
mode_of() { python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$1"; }
P="$(umask 022; launch_payload "$ROOT/ccd-new")"
ok "with no retention anywhere the launch keeps transcripts" \
   "$([ "$(effective_days "$P")" = 3650 ] && echo 0 || echo 1)"
ok "a config-dir settings file the seed creates is private" "$([ "$(mode_of "$ROOT/ccd-new/settings.json")" = 0o600 ] && echo 0 || echo 1)" \
   "$(mode_of "$ROOT/ccd-new/settings.json")"
echo '{"model": "x"}' > "$ROOT/ccd-0600/settings.json"; chmod 600 "$ROOT/ccd-0600/settings.json"
(umask 022; launch_payload "$ROOT/ccd-0600" >/dev/null)
ok "the seed keeps a 0600 settings file 0600" "$([ "$(mode_of "$ROOT/ccd-0600/settings.json")" = 0o600 ] && echo 0 || echo 1)" \
   "$(mode_of "$ROOT/ccd-0600/settings.json")"

echo "claude-hooks-launch-scope: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
