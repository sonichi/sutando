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
#   bash scripts/uninstall-core-pool.sh

set -euo pipefail

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_VAL="$(id -u)"
DOMAIN="gui/$UID_VAL"

removed=0
shopt -s nullglob
for plist in "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do
  base="$(basename "$plist")"
  label="${base%.plist}"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$plist"
  # persistent-form follower session outlives the wrapper; end it too
  idx="${label#com.sutando.core-}"
  case "$idx" in *[!0-9]*) ;; *) tmux kill-session -t "core-$idx" 2>/dev/null || true;; esac
  echo "removed: $base"
  removed=$((removed + 1))
done
shopt -u nullglob

if [ "$removed" -eq 0 ]; then
  echo "no pool members installed; nothing to remove"
else
  echo
  echo "Removed $removed pool member(s)."
  echo "Logs preserved under \$SUTANDO_WORKSPACE/logs/core-*.log (delete manually if not needed)."
fi
