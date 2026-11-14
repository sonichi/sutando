#!/bin/bash
# Local HTTP service helpers shared by startup.sh and the Codex core launcher, so a
# core started without startup.sh still serves the local agent API.

# LISTEN alone can be a wedged server: probe a cheap unknown path (never "/", which runs
# health-check.py) and kill a listener that answers nothing within the deadline.
reap_wedged_listener() {
  local port="$1" name="$2" rc=0
  lsof -i :"$port" -sTCP:LISTEN > /dev/null 2>&1 || return 0
  curl -s -o /dev/null -m 10 "http://127.0.0.1:$port/__liveness_probe__" || rc=$?
  if [ "$rc" -eq 28 ]; then
    echo "  ⚠ $name (port $port) listening but unresponsive — killing wedged listener"
    lsof -ti :"$port" -sTCP:LISTEN | xargs kill 2>/dev/null || true
    sleep 1
  fi
  return 0
}

# ensure_agent_api <repo> <python> <logs-dir>: start src/agent-api.py (port 7843)
# unless something already serves it. An empty <python> skips the start.
ensure_agent_api() {
  local repo="$1" py="$2" logs="$3"
  reap_wedged_listener 7843 agent-api
  if lsof -i :7843 > /dev/null 2>&1; then
    echo "  ✓ agent API (already running)"
    return 0
  fi
  if [ -z "$py" ]; then
    echo "  ~ agent API skipped (no runnable python3)"
    return 0
  fi
  echo "  Starting agent API (port 7843)..."
  mkdir -p "$logs" 2>/dev/null || true
  (cd "$repo" && exec nohup "$py" src/agent-api.py > "$logs/agent-api.log" 2>&1 < /dev/null) &
  echo "  ✓ agent API"
}
