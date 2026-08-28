#!/bin/bash
# Uninstall the multi-core agent pool — stops every pool plist and removes
# the files. Two flavors are installed per slot and both are torn down here:
#   com.sutando.core-<N>.plist            — the claude session
#   com.sutando.core-<N>-heartbeat.plist  — the .alive heartbeat sidecar
# The glob `com.sutando.core-[0-9]*.plist` catches both because the heartbeat
# label starts with a digit immediately after `core-`. Logs under
# $SUTANDO_WORKSPACE/logs/core-*.log{,err} are kept (not deleted) so
# post-mortem of any pool incident remains possible.
#
# CRITICAL: the glob pattern is `com.sutando.core-[0-9]*.plist`, NOT
# `com.sutando.core-*.plist`. The narrower pattern is intentional — the
# wider one would also match `com.sutando.core-agent.plist`, which is the
# pre-existing Sutando production launchd agent (managed by Sutando.app),
# NOT a pool member. Removing it would tear down the menu-bar app's agent
# liaison. Regression caught 2026-05-18 23:14 PT during script self-test;
# the recovery was `cp .plist.bak .plist && launchctl bootstrap`. Don't
# loosen this glob without an explicit allowlist-of-pool-members check.
#
# Idempotent: safe to run when no pool is installed.
#
# Usage:
#   bash scripts/uninstall-core-pool.sh [--only-core=<N>]
#
# --only-core removes ONE core and leaves the lead and every other core running.
# Removing a core is three steps, not one: `launchctl bootout` alone leaves the
# plist behind, and kick-pool revives any installed plist whose session is gone,
# while a stale state/cores/core-<N>.alive keeps the lead assigning to a core
# that no longer exists.

set -euo pipefail

ONLY_CORE=""
for arg in "$@"; do
  case "$arg" in
    --only-core=*)
      ONLY_CORE="${arg#--only-core=}"
      case "$ONLY_CORE" in
        ''|*[!0-9]*) echo "error: --only-core expects a positive integer; got '$ONLY_CORE'" >&2; exit 2 ;;
      esac ;;
    *) echo "error: unknown option '$arg'" >&2; exit 2 ;;
  esac
done

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_VAL="$(id -u)"
DOMAIN="gui/$UID_VAL"
WORKSPACE="$(bash "$(dirname "$0")/sutando-config.sh" workspace)"
WORKSPACE="${WORKSPACE/#\~/$HOME}"

remove_core() {
  local plist="$1" base label idx
  base="$(basename "$plist")"
  label="${base%.plist}"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$plist"
  # persistent-form follower session outlives the wrapper; end it too
  idx="${label#com.sutando.core-}"
  case "$idx" in
    *[!0-9]*) ;;
    *)
      tmux kill-session -t "core-$idx" 2>/dev/null || true
      # A left-behind beat file keeps the lead assigning work to a dead core.
      rm -f "$WORKSPACE/state/cores/core-$idx.alive" ;;
  esac
  echo "removed: $base"
}

removed=0
if [ -n "$ONLY_CORE" ]; then
  PLIST="$LAUNCH_AGENTS/com.sutando.core-$ONLY_CORE.plist"
  if [ -f "$PLIST" ]; then
    remove_core "$PLIST"
    removed=$((removed + 1))
  else
    # No plist, but the session and the .alive file can still be live.
    tmux kill-session -t "core-$ONLY_CORE" 2>/dev/null || true
    rm -f "$WORKSPACE/state/cores/core-$ONLY_CORE.alive"
    echo "no plist for com.sutando.core-$ONLY_CORE; cleared its session and beat"
  fi
  echo
  echo "Removed core-$ONLY_CORE (lead and other cores untouched)."
  exit 0
fi

shopt -s nullglob
for plist in "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do
  remove_core "$plist"
  removed=$((removed + 1))
done
shopt -u nullglob

# The lead is a pool member too — leaving its KeepAlive job behind would keep
# restarting a daemon whose followers are gone.
LEAD_PLIST="$LAUNCH_AGENTS/com.sutando.pool-lead.plist"
if [ -f "$LEAD_PLIST" ]; then
  launchctl bootout "$DOMAIN/com.sutando.pool-lead" 2>/dev/null || true
  rm -f "$LEAD_PLIST"
  echo "removed: com.sutando.pool-lead.plist"
  removed=$((removed + 1))
fi

if [ "$removed" -eq 0 ]; then
  echo "no pool members installed; nothing to remove"
else
  echo
  echo "Removed $removed pool member(s)."
  echo "Logs preserved under \$SUTANDO_WORKSPACE/logs/core-*.log (delete manually if not needed)."
fi
