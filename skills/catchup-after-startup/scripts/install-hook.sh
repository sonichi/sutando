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
# Safe to re-run — already-installed hooks are detected + skipped.
set -euo pipefail

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

python3 <<PYEOF
import json, sys
p = "$SETTINGS"
cmd = '''$HOOK_CMD'''
s = json.load(open(p))
hooks = s.setdefault('hooks', {})

# Migration (PR #1366 followup): pre-rename installs registered the hook under
# the invalid event name 'SessionStop' — Claude Code silently no-op'd them
# ("Unknown hook event 'SessionStop' was ignored"). Detect any stale
# SessionStop entry whose command references session-handoff.sh and drop it;
# the install path below adds a fresh SessionEnd entry with the current
# resolved REPO. Conservative: leaves unrelated SessionStop hooks alone.
ss_old = hooks.get('SessionStop', [])
migrated = 0
ss_old_clean = []
for g in ss_old:
    other_hooks = []
    for h in (g.get('hooks') or []):
        c = (h.get('command') or '') if h.get('type') == 'command' else ''
        if 'session-handoff.sh' in c:
            migrated += 1
        else:
            other_hooks.append(h)
    if other_hooks:
        ss_old_clean.append({'hooks': other_hooks})
if migrated:
    if ss_old_clean:
        hooks['SessionStop'] = ss_old_clean
    else:
        hooks.pop('SessionStop', None)
    print(f"migrated {migrated} stale session-handoff.sh hook(s) from SessionStop → SessionEnd")

ss = hooks.setdefault('SessionEnd', [])

# Match the existing shape: list of {hooks: [{type:command, command:...}]} groups.
# We add a single group with our one command, unless an equivalent already exists.
def has_cmd(groups, cmd):
    for g in groups:
        for h in (g.get('hooks') or []):
            if h.get('type') == 'command' and (h.get('command') or '').strip() == cmd.strip():
                return True
    return False

if has_cmd(ss, cmd) and not migrated:
    print("SessionEnd hook already installed — no changes")
elif has_cmd(ss, cmd):
    json.dump(s, open(p, 'w'), indent=2)
    print("SessionEnd hook already present; settings.json rewritten to drop the stale SessionStop entry")
else:
    ss.append({'hooks': [{'type': 'command', 'command': cmd}]})
    json.dump(s, open(p, 'w'), indent=2)
    print("installed SessionEnd hook → " + p)
    print("hook command:", cmd)
PYEOF
