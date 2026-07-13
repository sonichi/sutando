#!/usr/bin/env bash
# sutando-whoami.sh — print this Sutando instance's identity as one JSON object.
#
# The stable, machine-readable handle for "which Sutando is this?" — the thing
# a user pastes (or a tool reads) when another system needs to find or attach
# to this instance. First consumer: agent-connect's `--sutando-workspace`
# connect path (the AG2 Space Connect modal pre-fills from this output).
# Owner-designed primitive, 2026-07-13.
#
#   bash scripts/sutando-whoami.sh
#
# Output (single JSON object on stdout):
#   instance_id  stable per-host install id (state/auth/device.json machineId;
#                falls back to "unprovisioned-<host>" before provisioning)
#   host         short hostname (matches the hosts/<hostname>/ convention)
#   workspace    absolute workspace path (the M0 resolver's answer)
#   repo         absolute path of this checkout
#
# Agent identity (the @...:ag2.space mxid) is deliberately absent: it is not
# stored locally today — the gateway resolves token → identity server-side.
# When a gateway whoami op exists, it belongs here too.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace)"

python3 - "$REPO" "$WS" <<'PY'
import json
import os
import socket
import sys

repo, ws = sys.argv[1], sys.argv[2]
host = socket.gethostname().split(".")[0]

machine_id = None
try:
    with open(os.path.join(ws, "state", "auth", "device.json")) as f:
        machine_id = json.load(f).get("machineId")
except (OSError, ValueError):
    pass

print(
    json.dumps(
        {
            "instance_id": machine_id or f"unprovisioned-{host}",
            "host": host,
            "workspace": ws,
            "repo": repo,
        },
        indent=2,
    )
)
PY
