#!/usr/bin/env bash
# Idempotently wire the PERSONAL_CLAUDE.md compaction re-inject hook into the
# project-level .claude/settings.json. Called by startup.sh and safe to run
# standalone. Mirrors scripts/install-session-start-hook.sh — same target-dir
# resolution, same idempotent merge — but registers under the SessionStart
# "compact" matcher only: startup/resume are covered by the session-start Read
# (CLAUDE.md "Personal overrides"); compaction is the gap this hook closes.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck disable=SC1091
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"
if [ -z "$PY" ]; then
  echo "  ⚠ no runnable python3 — skipping PERSONAL_CLAUDE compact-reinject hook install" >&2
  exit 0
fi

# Target the directory the core `claude` process actually launches from — that
# is where Claude Code reads project-scoped `.claude/settings.json`. Same
# resolution as install-session-start-hook.sh: SUTANDO_CLAUDE_WORKING_DIR when
# set (expanded + physically resolved the way start-cli.sh does), else $REPO.
if [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ]; then
  _cwd_exp="${SUTANDO_CLAUDE_WORKING_DIR/#\~/$HOME}"
  mkdir -p "$_cwd_exp" || { echo "  ✗ can't create core working dir: $_cwd_exp" >&2; exit 1; }
  TARGET_DIR="$(cd "$_cwd_exp" && pwd -P)"
else
  TARGET_DIR="$REPO"
fi

SETTINGS="$TARGET_DIR/.claude/settings.json"
# The hint script itself always lives in this checkout ($REPO) — the working
# dir may be a different tree, but this is the checkout that ran startup.sh and
# is known to contain the script.
HINT_SCRIPT="$REPO/src/personal-claude-compact-hint.sh"
HOOK_CMD="bash \"$HINT_SCRIPT\""

mkdir -p "$TARGET_DIR/.claude"

if [ ! -f "$SETTINGS" ]; then
  echo '{"hooks":{}}' > "$SETTINGS"
fi

# Idempotent merge (avoids jq dependency). `python3 -` (not `/dev/stdin`):
# /dev/stdin is a silent no-op under some sandboxed environments (caught in
# review on this PR — the merge never ran and settings.json stayed {"hooks":{}}),
# while `-` reads the program from stdin portably and matches the hint
# script's own pattern.
"$PY" - "$SETTINGS" "$HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

# Already present in any entry → no-op
for entry in session_start:
    for h in entry.get("hooks", []):
        if h.get("command", "") == hook_cmd:
            print("  ✓ PERSONAL_CLAUDE compact-reinject hook (already installed)")
            sys.exit(0)

# "compact" matcher: fire ONLY after context compaction, not on
# startup/resume/clear — those paths already Read the file per CLAUDE.md.
session_start.append({
    "matcher": "compact",
    "hooks": [{"type": "command", "command": hook_cmd}]
})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("  ✓ PERSONAL_CLAUDE compact-reinject hook (installed)")
PYEOF
