#!/bin/bash
# spawn-worker.sh — leave single-worker mode by adding workers to the pool.
#
# Single-worker mode is the default: the core is the only worker and no
# com.sutando.core-N launchd job exists. Spawning installs (or grows) the
# lead-follower pool through scripts/install-core-pool.sh, which also ensures
# the pool lead, so one command takes an install from "just the core" to
# multi-worker mode.
#
# Usage:
#   bash skills/spawn-worker/scripts/spawn-worker.sh [--count K | --to N]
#                                                   [--status] [--dry-run]
#
#   (no flags)   add ONE worker to whatever is installed (0 -> 1, 3 -> 4)
#   --count K    add K workers instead of one
#   --to N       resize to exactly N workers; N below the installed size is
#                refused (scale-down is manual: scripts/uninstall-core-pool.sh)
#   --status     print the current mode and exit; touches nothing
#   --dry-run    print the plan (installed, target, command) and exit 0
#
# Exit codes: 0 done, 2 usage/refused, otherwise the installer's own code.
#
# Refuses when SUTANDO_CORE_ID is set: a pool worker re-running the installer
# reboots its own supervisor. Run it from the core (or a plain shell) instead.
set -u

SUTANDO_SPAWN_WAIT_S="${SUTANDO_SPAWN_WAIT_S:-20}"

# Installed skills are symlinks, so dirname "$0" points into the skills home,
# not the repo; resolve the real path before walking up.
_self="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")"
REPO="${SUTANDO_ROOT:-$(cd "$(dirname "$_self")/../../.." && pwd)}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

installed_workers() {
  local n=0 p
  shopt -s nullglob
  for p in "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do n=$((n + 1)); done
  shopt -u nullglob
  echo "$n"
}

lead_state() {
  [ -f "$LAUNCH_AGENTS/com.sutando.pool-lead.plist" ] && echo "installed" || echo "missing"
}

live_workers() {
  # A beat younger than 90s is the pool's own liveness rule.
  local ws n=0 f age now
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || { echo 0; return; }
  now="$(date +%s)"
  shopt -s nullglob
  for f in "$ws"/state/cores/core-[0-9]*.alive; do
    age=$(( now - $(stat -f %m "$f" 2>/dev/null || echo 0) ))
    [ "$age" -lt 90 ] && n=$((n + 1))
  done
  shopt -u nullglob
  echo "$n"
}

print_status() {
  local n
  n="$(installed_workers)"
  if [ "$n" -eq 0 ]; then
    echo "mode=single-worker workers=0 lead=$(lead_state)"
  else
    echo "mode=multi-worker workers=$n live=$(live_workers) lead=$(lead_state)"
  fi
}

COUNT=1
TO=""
STATUS=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status) STATUS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --count) shift; COUNT="${1:-}" ;;
    --count=*) COUNT="${1#--count=}" ;;
    --to) shift; TO="${1:-}" ;;
    --to=*) TO="${1#--to=}" ;;
    -h|--help) sed -n '2,24p' "$_self" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
  shift
done

if [ "$STATUS" -eq 1 ]; then
  print_status
  exit 0
fi

case "$COUNT" in ''|*[!0-9]*|0) echo "error: --count needs a positive integer; got '$COUNT'" >&2; exit 2 ;; esac
if [ -n "$TO" ]; then
  case "$TO" in ''|*[!0-9]*|0) echo "error: --to needs a positive integer; got '$TO'" >&2; exit 2 ;; esac
fi

if [ -n "${SUTANDO_CORE_ID:-}" ]; then
  echo "refused: this is pool worker core-${SUTANDO_CORE_ID}; spawning from inside a worker reboots its own supervisor. Run from the core." >&2
  exit 2
fi

INSTALLED="$(installed_workers)"
if [ -n "$TO" ]; then
  TARGET="$TO"
else
  TARGET=$((INSTALLED + COUNT))
fi
if [ "$TARGET" -lt "$INSTALLED" ]; then
  echo "refused: --to $TARGET is below the installed $INSTALLED workers; scale-down is manual (scripts/uninstall-core-pool.sh)." >&2
  exit 2
fi
if [ "$TARGET" -eq "$INSTALLED" ]; then
  echo "nothing to do: $INSTALLED workers already installed ($(print_status))"
  exit 0
fi

INSTALLER="$REPO/scripts/install-core-pool.sh"
[ -f "$INSTALLER" ] || { echo "error: installer not found at $INSTALLER" >&2; exit 2; }

echo "plan: installed=$INSTALLED target=$TARGET mode-after=multi-worker"
echo "command: bash scripts/install-core-pool.sh $TARGET"
if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

# The full install boots out the lead and every worker job; the tmux sessions
# outlive the jobs, so a running worker keeps its context across the churn.
bash "$INSTALLER" "$TARGET"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "spawn-worker: installer exited $rc; installed now: $(installed_workers)" >&2
  exit "$rc"
fi

if [ "$SUTANDO_SPAWN_WAIT_S" -gt 0 ] 2>/dev/null; then
  sleep "$SUTANDO_SPAWN_WAIT_S"
fi
echo "done: $(print_status)"
