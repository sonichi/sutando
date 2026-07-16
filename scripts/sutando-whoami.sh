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
#   agent_id     the @…:ag2.space mxid, read LOCALLY from the device-auth env
#                (<config_dir>/channels/ag2space/.env AGENT_ID) that the connect
#                flow writes; null before a device is connected
#   workspace    absolute workspace path — the USER DATA folder (M0 resolver)
#   repo         absolute path of this checkout (the CODE)
#   config_dir   absolute .claude-sutando config dir (sessions/memory/channels)
#   runtime      { core_running, gateway_running, tmux_socket, session } — is a
#                sutando-core actually up on this host, and where (the "where are
#                the instances" signal). Best-effort; never fails the whoami.
#
# Identity note: agent_id is read LOCALLY from the device-auth env — that is where
# the connect flow persists it. If it is absent the device is unprovisioned; a
# future gateway whoami op can additionally cross-check token → identity server-side.
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
if command -v tmux >/dev/null 2>&1 && tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
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

# Agent identity: read AGENT_ID from the device-auth env — the file the connect
# flow writes (channels/ag2space/.env). Absent before a device is connected.
agent_id = None
if config_dir:
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
