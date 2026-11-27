#!/usr/bin/env bash
# A worker seat whose Claude config holds an MCP entry on a server that moved is told so at launch:
# worker_mcp_moved_servers warns once per entry on stderr, never fails the launch and never edits
# the config; the launcher calls it after the seat's Claude folder is final.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PY="$(command -v python3)"
read -r OLD NEW OLD_MCP NEW_MCP < <("$PY" - "$REPO" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1] + "/src")
import dotspace
old, new = dotspace.LEGACY_SERVER, dotspace.SUCCESSOR_SERVER
print(old, new, dotspace.dotspace_for(old, fetch=dotspace.NO_FETCH).mcp_url,
      dotspace.dotspace_for(new, fetch=dotspace.NO_FETCH).mcp_url)
PYEOF
)
# shellcheck disable=SC1091
. "$REPO/skills/worker-pool/scripts/worker-mcp-credential.sh"

mkdir -p "$TMP/moved" "$TMP/live" "$TMP/none"
printf '{"mcpServers": {"Old-Space": {"type": "http", "url": "%s"}, "live": {"type": "http", "url": "%s"}}}\n' \
  "$OLD_MCP" "$NEW_MCP" > "$TMP/moved/.claude.json"
printf '{"mcpServers": {"live": {"type": "http", "url": "%s"}}}\n' "$NEW_MCP" > "$TMP/live/.claude.json"
before="$(cksum < "$TMP/moved/.claude.json")"

worker_mcp_moved_servers "$REPO" "$PY" "$TMP/moved" > "$TMP/out" 2> "$TMP/err"
check $? "a moved entry does not fail the launch"
[ ! -s "$TMP/out" ]
check $? "nothing goes to stdout"
[ "$(grep -c "⚠" "$TMP/err")" = "1" ] && grep -qF "⚠ MCP server \"Old-Space\" connects to $OLD, which moved to $NEW ($NEW_MCP)" "$TMP/err"
check $? "one warning names the entry, the server it is on and where that server moved"
grep -qF 'claude mcp remove --scope user "Old-Space"' "$TMP/err"
check $? "the warning gives the remove command"
[ "$(cksum < "$TMP/moved/.claude.json")" = "$before" ]
check $? "the config is left as it was"

for dir in live none; do
  worker_mcp_moved_servers "$REPO" "$PY" "$TMP/$dir" > "$TMP/out" 2> "$TMP/err"
  rc=$?
  [ "$rc" = "0" ] && [ ! -s "$TMP/out" ] && [ ! -s "$TMP/err" ]
  check $? "a seat with no moved entry ($dir) is silent"
done

worker_mcp_moved_servers "$REPO" "$TMP/no-such-python" "$TMP/moved" > "$TMP/out" 2> "$TMP/err"
check $? "an interpreter that cannot run does not fail the launch"

LAUNCHER="$REPO/skills/worker-pool/scripts/launch-worker-session.sh"
call="$(grep -n '^worker_mcp_moved_servers "\$REPO" "\$WORKER_PY" "\${CLAUDE_CONFIG_DIR:-}"$' "$LAUNCHER" | cut -d: -f1)"
folder="$(grep -n 'export CLAUDE_CONFIG_DIR="\$_own_ccd"' "$LAUNCHER" | cut -d: -f1)"
start="$(grep -n '^if ! launch_claude_session; then' "$LAUNCHER" | cut -d: -f1)"
[ -n "$call" ] && [ -n "$folder" ] && [ -n "$start" ] && [ "$folder" -lt "$call" ] && [ "$call" -lt "$start" ]
check $? "the launcher checks the seat's final Claude folder before it starts the session"

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
