#!/bin/bash
# Claude Code SessionStart hook — Agent Registry registration.
#
# Registers this Claude Code instance with the local Agent Registry service
# and heartbeats until the session ends. The service is auto-started if it is
# not already running (--autostart).
#
# Install: add to .claude/settings.json under hooks.SessionStart, e.g.
#   { "hooks": { "SessionStart": [
#       { "hooks": [ { "type": "command",
#         "command": "bash <skill>/hooks/session-start.sh" } ] } ] } }
#
# The registration process is backgrounded so it never delays session start.
# It lives in the session's process group: when Claude Code exits, it receives
# SIGTERM and deregisters cleanly. If it is killed ungracefully, the registry
# ages the entry out via heartbeat staleness instead.

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$SKILL_DIR/scripts/registry-client.py" watch \
  --name "claude-code" \
  --cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
  --pid "$PPID" \
  --interval 30 \
  --autostart \
  >/dev/null 2>&1 &

exit 0
