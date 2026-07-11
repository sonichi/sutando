#!/bin/bash
# COMPAT SHIM — DO NOT ADD LOGIC HERE.
#
# The canonical launcher moved to src/agent/claude/cli/start-cli.sh (PR #1891).
# This wrapper preserves the old path for ONE RELEASE so callers that still
# reference scripts/start-cli.sh keep working until they pick up the new path —
# notably a not-yet-rebuilt Sutando.app binary (the path is compiled in, so it
# lags a `git pull`) or an in-flight health-check --recover-core. Remove after
# one release once all callers reference src/agent/claude/cli/start-cli.sh.
_repo="$(cd "$(dirname "$0")/.." && pwd)"
echo "scripts/start-cli.sh is a compat shim — moved to src/agent/claude/cli/start-cli.sh (update your caller)." >&2
exec bash "$_repo/src/agent/claude/cli/start-cli.sh" "$@"
