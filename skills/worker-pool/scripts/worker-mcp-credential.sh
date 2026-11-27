#!/usr/bin/env bash
# Sourced by both worker launchers. worker_mcp_credential REPO PYTHON WORKER [WORKSPACE] prints the seat's
# AG2_MCP_WORKER_CREDENTIAL; a failed compose prints `[]`, a seat with no resolver, which the proxy refuses.
worker_mcp_credential() {
  local repo="$1" py="$2" worker="$3" ws="${4:-}" err out
  err="$(mktemp)"
  out="$("${py:-python3}" "$repo/scripts/connections.py" ${ws:+--workspace "$ws"} worker-mcp-credential-argv \
    --worker "$worker" --repo "$repo" --python "$py" 2>"$err")" \
    || { out="[]"; echo "  ⚠ no MCP credential resolver for this worker ($(head -n 1 "$err")); its s-space MCP tools will refuse rather than act as the core" >&2; }
  [ -n "$out" ] || out="[]"
  rm -f "$err"
  printf '%s\n' "$out"
}

# worker_mcp_moved_servers REPO PYTHON [CLAUDE_CONFIG_DIR] warns, one line each, about the seat's MCP
# entries that connect to a server that moved; it never fails the launch and never edits the config.
worker_mcp_moved_servers() {
  local repo="$1" py="$2" ccd="${3:-}" line
  "${py:-python3}" "$repo/scripts/connections.py" moved-mcp-servers ${ccd:+--mcp-config "$ccd/.claude.json"} 2>/dev/null \
    | while IFS= read -r line; do [ -z "$line" ] || echo "  ⚠ $line" >&2; done
  return 0
}
