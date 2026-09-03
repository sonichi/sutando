#!/bin/bash
# Uninstall the multi-worker agent pool — stops every pool plist and removes
# the files. Two flavors can be installed per seat and both are torn down here:
#   com.sutando.worker-<N>.plist          — the worker session
#   com.sutando.core-<N>.plist            — the same seat under its legacy name
# (plus the retired `-heartbeat` sidecar of either). Logs under the pool log
# dir (`<worker>.log{,.err}`) are kept so post-mortem of any pool incident
# remains possible.
#
# CRITICAL: the legacy glob is `com.sutando.core-[0-9]*.plist`, NOT
# `com.sutando.core-*.plist`. The wider one would also match
# `com.sutando.core-agent.plist`, which is the pre-existing Sutando production
# launchd agent (managed by Sutando.app), NOT a pool member. Removing it would
# tear down the menu-bar app's agent liaison. Regression caught 2026-05-18
# 23:14 PT during script self-test; the recovery was `cp .plist.bak .plist &&
# launchctl bootstrap`. Don't loosen this glob without an explicit
# allowlist-of-pool-members check. `com.sutando.worker-[0-9]*` has no such
# neighbour; the digit anchor is kept for symmetry.
#
# Idempotent: safe to run when no pool is installed.
#
# Usage:
#   bash scripts/uninstall-worker-pool.sh [--only-worker=<N>]
#
# --only-worker removes ONE worker (both spellings of its seat) and leaves the
# lead and every other worker running. Removing a worker is three steps, not
# one: `launchctl bootout` alone leaves the plist behind, and kick-pool revives
# any installed plist whose session is gone, while a stale
# state/cores/<worker>.alive keeps the lead assigning to a worker that no
# longer exists. `--only-core=` is accepted as a one-release alias.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Worker naming has one owner; bash never spells `worker-N` itself.
pool_name() { python3 "$REPO_DIR/src/pool_names.py" "$@"; }

ONLY_WORKER=""
for arg in "$@"; do
  case "$arg" in
    --only-worker=*|--only-core=*)
      case "$arg" in --only-core=*) echo "note: --only-core is deprecated; use --only-worker" >&2 ;; esac
      ONLY_WORKER="${arg#*=}"
      case "$ONLY_WORKER" in
        ''|*[!0-9]*) echo "error: --only-worker expects a positive integer; got '$ONLY_WORKER'" >&2; exit 2 ;;
      esac ;;
    *) echo "error: unknown option '$arg'" >&2; exit 2 ;;
  esac
done

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_VAL="$(id -u)"
DOMAIN="gui/$UID_VAL"
WORKSPACE="$(bash "$(dirname "$0")/sutando-config.sh" workspace)"
WORKSPACE="${WORKSPACE/#\~/$HOME}"

# End a worker's session and beat by its instance name (either spelling).
clear_session_and_beat() {
  local inst="$1"
  # persistent-form follower session outlives the wrapper; end it too
  tmux kill-session -t "$inst" 2>/dev/null || true
  # A left-behind beat file keeps the lead assigning work to a dead worker.
  rm -f "$WORKSPACE/state/cores/$inst.alive"
}

remove_worker() {
  local plist="$1" base label inst
  base="$(basename "$plist")"
  label="${base%.plist}"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$plist"
  inst="${label#com.sutando.}"; inst="${inst%-heartbeat}"
  if [ -n "$(pool_name seat_of "$inst" || true)" ]; then
    clear_session_and_beat "$inst"
  fi
  echo "removed: $base"
}

removed=0
if [ -n "$ONLY_WORKER" ]; then
  WNAME="$(pool_name worker_name "$ONLY_WORKER")"
  for inst in "$WNAME" "$(pool_name legacy_name "$WNAME")"; do
    PLIST="$LAUNCH_AGENTS/com.sutando.$inst.plist"
    if [ -f "$PLIST" ]; then
      remove_worker "$PLIST"
      removed=$((removed + 1))
    else
      # No plist, but the session and the .alive file can still be live.
      clear_session_and_beat "$inst"
    fi
  done
  [ "$removed" -gt 0 ] || echo "no plist for com.sutando.$WNAME; cleared its session and beat"
  echo
  echo "Removed $WNAME (lead and other workers untouched)."
  exit 0
fi

shopt -s nullglob
for plist in "$LAUNCH_AGENTS"/com.sutando.worker-[0-9]*.plist \
             "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do
  remove_worker "$plist"
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
  echo "Logs preserved under the pool log dir (<worker>.log; delete manually if not needed)."
fi
