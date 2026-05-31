#!/usr/bin/env bash
# Idempotently install the SessionEnd hook that pairs with /catchup.
#
# /catchup reads from session-state.md to know what the previous session was
# doing. That file is written by `src/session-handoff.sh` — currently triggered
# ONLY by the PreCompact hook. If the previous session exited cleanly (⌘Q)
# without a compaction in between, the file stays at "last compact" instead
# of "last close", losing the most-recent session window.
#
# This hook makes session-handoff.sh also fire on SessionEnd, closing the
# gap.
#
# REPO resolution: the previous version baked `${SUTANDO_REPO_DIR:-$HOME/Desktop/sutando}`
# into the hook command verbatim, so machines without SUTANDO_REPO_DIR set
# AND without a checkout at ~/Desktop/sutando silently no-op'd every
# SessionEnd. (Real incident 2026-05-23 on Mac Studio.) We now resolve REPO
# at install time using the same probe heuristic as catchup-after-startup.sh
# and bake the literal path into the hook — no runtime probe burden, and a
# misconfig fails loudly at install instead of silently at hook fire.
#
# Pre-rename installs (before #1366) registered the hook under the invalid
# event name "SessionStop", which Claude Code silently no-ops. The universal
# key-rename migration lives in migrate-settings-hooks.py — we call it first
# so every stale SessionStop entry (not just our own) graduates to
# SessionEnd before we install. catchup-after-startup.sh also calls the same
# script so the rename self-heals on every fresh session, without the user
# having to re-run this installer.
#
# Safe to re-run — already-installed hooks are detected + skipped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${HOME}/.claude/settings.json"

# Resolve REPO at install time (env wins, else probe common layouts).
if [ -n "${SUTANDO_REPO_DIR:-}" ]; then
  REPO_FOR_HOOK="$SUTANDO_REPO_DIR"
else
  REPO_FOR_HOOK=""
  for _cand in "$HOME/Desktop/sutando" "$HOME/Documents/sutando/sutando" "$HOME/Documents/sutando" "$HOME/sutando" "$(pwd)"; do
    if [ -f "$_cand/CLAUDE.md" ] && [ -d "$_cand/skills" ] && [ -d "$_cand/.git" ]; then
      REPO_FOR_HOOK="$_cand"; break
    fi
  done
  if [ -z "$REPO_FOR_HOOK" ]; then
    echo "error: couldn't auto-detect sutando checkout — set SUTANDO_REPO_DIR and re-run" >&2
    echo "       (probed: \$HOME/Desktop/sutando, \$HOME/Documents/sutando/sutando, \$HOME/Documents/sutando, \$HOME/sutando, \$(pwd))" >&2
    exit 1
  fi
fi

# The hook command — literal resolved path, no runtime probe.
HOOK_CMD="bash \"$REPO_FOR_HOOK/src/session-handoff.sh\" \"\${TRANSCRIPT_PATH:-}\""

if [ ! -f "$SETTINGS" ]; then
  echo "error: $SETTINGS not found — Claude Code not configured on this machine?" >&2
  exit 1
fi

# Universal SessionStop -> SessionEnd migration (idempotent; quiet if nothing to do).
# Lives in its own script so catchup-after-startup.sh can auto-call it on every
# fresh session — see migrate-settings-hooks.py.
python3 "$HERE/migrate-settings-hooks.py" "$SETTINGS"

python3 <<PYEOF
import json, os
p = "$SETTINGS"
cmd = '''$HOOK_CMD'''
s = json.load(open(p))
hooks = s.setdefault('hooks', {})
ss = hooks.setdefault('SessionEnd', [])

# Match the existing shape: list of {hooks: [{type:command, command:...}]} groups.
# We add a single group with our one command, unless an equivalent already exists.
def has_cmd(groups, cmd):
    for g in groups:
        for h in (g.get('hooks') or []):
            if h.get('type') == 'command' and (h.get('command') or '').strip() == cmd.strip():
                return True
    return False

# Atomic write — sibling tmp + rename. settings.json is read by every Claude
# Code session; a half-written file breaks every shell. Mini's #1374 review catch.
def atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)

if has_cmd(ss, cmd):
    print("SessionEnd hook already installed — no changes")
else:
    ss.append({'hooks': [{'type': 'command', 'command': cmd}]})
    atomic_write(p, json.dumps(s, indent=2))
    print("installed SessionEnd hook → " + p)
    print("hook command:", cmd)
PYEOF
