#!/bin/bash
# COMPAT SHIM — DO NOT ADD LOGIC HERE.
#
# The canonical onboarding script moved to
# src/agent/claude/cli/sutando-shell-setup.sh (PR #1891). This wrapper preserves
# the old path for ONE RELEASE so old callers (docs, muscle-memory, an older
# migrate.sh, a not-yet-rebuilt Sutando.app) keep working until they pick up the
# new path. It is only ever RUN (never sourced) — `bash …/sutando-shell-setup.sh
# --auto|--import` — so exec-forwarding preserves behavior. Remove after one
# release.
_repo="$(cd "$(dirname "$0")/.." && pwd)"
echo "scripts/sutando-shell-setup.sh is a compat shim — moved to src/agent/claude/cli/sutando-shell-setup.sh (update your caller)." >&2
exec bash "$_repo/src/agent/claude/cli/sutando-shell-setup.sh" "$@"
