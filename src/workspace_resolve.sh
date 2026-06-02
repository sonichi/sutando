#!/bin/bash
# Shared workspace resolution for bash scripts. Source this file with:
#
#   source "$REPO/src/workspace_resolve.sh"
#   resolve_workspace_or_die  # exports WORKSPACE; exits non-zero on failure
#
# Single source for the post-M0 (PR #1395) resolution pattern. Replaces the
# `if helper; elif env; else fail` block previously duplicated across:
# init.sh, install-credential-proxy-launchd.sh, install-sutando-app-launchd.sh,
# install-health-check-launchd.sh, session-handoff.sh. Factored out per
# Lucy's PR #1399 review nit #1.
#
# Resolution order:
#   1. scripts/sutando-config.sh helper (M0 default = <repo>/workspace/, env
#      var honored internally as legacy escape hatch)
#   2. $SUTANDO_WORKSPACE env var (only when helper is unreachable —
#      non-checkout / extracted-tarball install)
#   3. Fail loud with exit 1 + diagnostic. Refuses to silently write to a
#      hardcoded legacy default.
#
# Caller contract: must export $REPO before sourcing or calling. The function
# exports WORKSPACE on success.

resolve_workspace_or_die() {
  local helper="${REPO:?resolve_workspace_or_die: \$REPO not set}/scripts/sutando-config.sh"
  if [ -f "$helper" ]; then
    if ! WORKSPACE="$(bash "$helper" workspace)"; then
      echo "${0##*/}: ${helper##*/} workspace exited non-zero." >&2
      exit 1
    fi
  elif [ -n "${SUTANDO_WORKSPACE:-}" ]; then
    WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
  else
    echo "${0##*/}: cannot resolve workspace — neither $helper exists nor \$SUTANDO_WORKSPACE is set." >&2
    exit 1
  fi
  if [ -z "$WORKSPACE" ]; then
    echo "${0##*/}: workspace resolved to empty string. Refusing to derive paths under /." >&2
    exit 1
  fi
  export WORKSPACE
}
