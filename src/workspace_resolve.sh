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
# Resolution order (v0.8 — env override removed):
#   1. scripts/sutando-config.sh helper (<repo>/workspace/ resolved via
#      sutando.config.{json,local.json} or the baked-in default).
#   2. Fail loud with exit 1 + diagnostic. Refuses to silently write to a
#      hardcoded legacy default OR a now-unhonored env var.
#
# Self-locating: looks for scripts/sutando-config.sh relative to THIS file's
# own location (${BASH_SOURCE[0]}), NOT $REPO. This makes the function
# cross-checkout safe — callers can be invoked with $SUTANDO_REPO_DIR pointed
# at a different checkout (e.g. submodule pin) without breaking helper
# resolution. Caught by E2E pass against PR #1399 — see commit log.

resolve_workspace_or_die() {
  local _wr_dir
  _wr_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local helper="$_wr_dir/../scripts/sutando-config.sh"
  if [ -f "$helper" ]; then
    if ! WORKSPACE="$(bash "$helper" workspace)"; then
      echo "${0##*/}: scripts/sutando-config.sh workspace exited non-zero." >&2
      exit 1
    fi
  else
    echo "${0##*/}: cannot resolve workspace — $helper does not exist. v0.8 contract requires the helper; \$SUTANDO_WORKSPACE is no longer honored." >&2
    exit 1
  fi
  if [ -z "$WORKSPACE" ]; then
    echo "${0##*/}: workspace resolved to empty string. Refusing to derive paths under /." >&2
    exit 1
  fi
  export WORKSPACE
}
