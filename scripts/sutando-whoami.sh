#!/usr/bin/env bash
# sutando-whoami.sh — print this Sutando instance's identity + runtime as one JSON object.
#
# The stable, machine-readable handle for "which Sutando is this, and is it
# running?" — the thing a user pastes (or a tool reads) when another system
# needs to find, attach to, or pre-fill a form about this instance. First
# consumer: agent-connect's `--sutando-workspace` connect path (the AG2 Space
# Connect modal pre-fills from this output). Owner-designed primitive, 2026-07-13.
#
#   bash scripts/sutando-whoami.sh
#
# Output (single JSON object on stdout):
#   instance_id  stable per-host install id (state/auth/device.json machineId;
#                falls back to "unprovisioned-<host>" before provisioning)
#   host         short hostname (matches the hosts/<hostname>/ convention)
#   agent_id     the agent mxid of the primary connection (state/connections.json
#                agent_mxid), else AGENT_ID from the device-auth env
#                (<config_dir>/channels/ag2space/.env) that the connect flow
#                writes; null before a device is connected
#   workspace    absolute workspace path — the USER DATA folder (M0 resolver)
#   repo         absolute path of this checkout (the CODE)
#   config_dir   absolute .claude-sutando config dir (sessions/memory/channels)
#   runtime      { core_running, gateway_running, tmux_socket, session } — is a
#                sutando-core actually up on this host, and where (the "where are
#                the instances" signal). Best-effort; never fails the whoami.
#
# Identity note: agent_id is read LOCALLY — the primary connection row first, so a
# move to another server reports the agent that now serves the owner, then the
# device-auth env. If both are absent the device is unprovisioned.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace)"

# Config dir (the .claude-sutando data dir). Best-effort — the resolver refuses
# on an invalid config, and whoami must still answer the rest, so swallow errors.
CONFIG_DIR="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null || true)"

# Runtime detection (best-effort — a whoami that crashes because tmux is missing
# is useless; each probe degrades to false). Mirrors start-cli.sh's socket env.
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="sutando-core"
CORE_RUNNING=false
if command -v tmux >/dev/null 2>&1 && tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
  CORE_RUNNING=true
fi
GATEWAY_RUNNING=false
if pgrep -f "remote-gateway-bridge" >/dev/null 2>&1; then
  GATEWAY_RUNNING=true
fi

python3 - "$REPO" "$WS" "$CONFIG_DIR" "$TMUX_SOCKET" "$SESSION" "$CORE_RUNNING" "$GATEWAY_RUNNING" <<'PY'
import json
import os
import socket
import sys

repo, ws, config_dir, tmux_socket, session, core_running, gateway_running = sys.argv[1:8]
host = socket.gethostname().split(".")[0]

machine_id = None
try:
    with open(os.path.join(ws, "state", "auth", "device.json")) as f:
        machine_id = json.load(f).get("machineId")
except (OSError, ValueError):
    pass

# The primary row's agent, else AGENT_ID from the device-auth env the connect flow writes.
agent_id = None
try:
    sys.path.insert(0, os.path.join(repo, "src"))
    import connections

    agent_id = ((connections.primary_row(ws) or {}).get("agent_mxid") or "").strip() or None
except Exception:
    pass
if config_dir and not agent_id:
    env_path = os.path.join(config_dir, "channels", "ag2space", ".env")
    try:
        with open(env_path) as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("AGENT_ID="):
                    agent_id = line.split("=", 1)[1].strip().strip('"').strip("'") or None
                    break
    except OSError:
        pass

print(
    json.dumps(
        {
            "instance_id": machine_id or f"unprovisioned-{host}",
            "host": host,
            "agent_id": agent_id,
            "workspace": ws,
            "repo": repo,
            "config_dir": config_dir or None,
            "runtime": {
                "core_running": core_running == "true",
                "gateway_running": gateway_running == "true",
                "tmux_socket": tmux_socket,
                "session": session,
            },
        },
        indent=2,
    )
)
PY
