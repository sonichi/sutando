#!/usr/bin/env bash
# Idempotently install the SessionStop hook that pairs with /catchup.
#
# /catchup reads from session-state.md to know what the previous session was
# doing. That file is written by `src/session-handoff.sh` — currently triggered
# ONLY by the PreCompact hook. If the previous session exited cleanly (⌘Q)
# without a compaction in between, the file stays at "last compact" instead
# of "last close", losing the most-recent session window.
#
# This hook makes session-handoff.sh also fire on SessionStop, closing the
# gap. Required env: SUTANDO_REPO_DIR pointing at the Sutando checkout (so
# the hook can locate session-handoff.sh). Default: ~/Desktop/sutando.
#
# Safe to re-run — already-installed hooks are detected + skipped.
set -euo pipefail

SETTINGS="${HOME}/.claude/settings.json"
# The hook command — points at the script that ships with Sutando.
HOOK_CMD='bash "${SUTANDO_REPO_DIR:-$HOME/Desktop/sutando}/src/session-handoff.sh" "${TRANSCRIPT_PATH:-}"'

if [ ! -f "$SETTINGS" ]; then
  echo "error: $SETTINGS not found — Claude Code not configured on this machine?" >&2
  exit 1
fi

python3 <<PYEOF
import json, sys
p = "$SETTINGS"
cmd = '''$HOOK_CMD'''
s = json.load(open(p))
hooks = s.setdefault('hooks', {})
ss = hooks.setdefault('SessionStop', [])

# Match the existing shape: list of {hooks: [{type:command, command:...}]} groups.
# We add a single group with our one command, unless an equivalent already exists.
def has_cmd(groups, cmd):
    for g in groups:
        for h in (g.get('hooks') or []):
            if h.get('type') == 'command' and (h.get('command') or '').strip() == cmd.strip():
                return True
    return False

if has_cmd(ss, cmd):
    print("SessionStop hook already installed — no changes")
else:
    ss.append({'hooks': [{'type': 'command', 'command': cmd}]})
    json.dump(s, open(p, 'w'), indent=2)
    print("installed SessionStop hook → " + p)
    print("hook command:", cmd)
PYEOF
