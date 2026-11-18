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
