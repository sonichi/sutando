#!/bin/bash
# COMPAT SHIM — DO NOT ADD LOGIC HERE.
#
# The canonical core launcher is src/agent/start-cli.sh (PR #1891). This wrapper
# preserves the old path for callers that were not updated in lockstep — notably
# a not-yet-rebuilt Sutando.app, whose backend-supervisor.mjs still resolves
# scripts/start-cli.sh and silently no-ops when it is absent.
_repo="$(cd "$(dirname "$0")/.." && pwd)"
echo "scripts/start-cli.sh is a compat shim — moved to src/agent/start-cli.sh (update your caller)." >&2
exec bash "$_repo/src/agent/start-cli.sh" "$@"
