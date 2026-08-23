#!/bin/bash
# Install the multi-core agent pool — N launchd-managed claude sessions
# that share one workspace and coordinate via the claim primitive (#880).
#
# Usage:
#   bash scripts/install-core-pool.sh [N]
#
# Defaults to N=3 per #881 design (owner directive 2026-05-18: "Set N=3 by default").
# Set N=1 to disable parallelism while keeping the plumbing installed.
#
# Idempotent: re-running with the same N updates plists in place; re-running
# with a different N removes excess plists and creates new ones to match.
#
# Pre-flight checks: claude CLI on PATH, resolved workspace writable.
# Each plist runs `claude --print "/proactive-loop-pool"` — the pool-aware
# variant of the proactive-loop skill that wedges the claim call before
# task pickup. The pool-aware skill is OUT OF THIS PR's scope; install the
# plists with N=1 first to verify the launchd shape, then bump to N>1 once
# the skill is in place.

set -euo pipefail

N="${1:-3}"
case "$N" in
  ''|*[!0-9]*) echo "error: N must be a positive integer; got '$N'" >&2; exit 2 ;;
esac
if [ "$N" -lt 1 ] || [ "$N" -gt 16 ]; then
  echo "error: N must be in [1, 16]; got $N" >&2
  exit 2
fi

# Resolve repo root first: the preflight below and the staging block both
# address repo files by absolute path. This script lives at
# `<repo>/scripts/install-core-pool.sh`.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve workspace via the canonical default (matches every other Sutando
# component). Resolved via the M0 helper (env override retired post-#1440).
WORKSPACE="$(bash "$(dirname "$0")/sutando-config.sh" workspace)"
# capture the installer's PATH: launchd strips env, and the sessions need brew bins
POOL_PATH="${PATH}"
# followers must share the LIVE session's credential store — a defaulted
# ~/.claude may hold expired OAuth (measured: launchd session auth-failed)
CLAUDE_CONFIG_DIR_EFFECTIVE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Preflight: followers fail with opaque one-liners when these are wrong —
# check here where the operator can still see and fix them.
if [ ! -f "$CLAUDE_CONFIG_DIR_EFFECTIVE/.credentials.json" ]; then
  echo "WARN: no credentials at $CLAUDE_CONFIG_DIR_EFFECTIVE (followers will fail auth)" >&2
fi
mkdir -p "$CLAUDE_CONFIG_DIR_EFFECTIVE/skills"
ln -sfn "$REPO_DIR/skills/proactive-loop-pool" "$CLAUDE_CONFIG_DIR_EFFECTIVE/skills/proactive-loop-pool"
WORKSPACE="${WORKSPACE/#\~/$HOME}"
mkdir -p "$WORKSPACE/logs"
mkdir -p "$WORKSPACE/state/cores"

# TCC: launchd cannot exec scripts under ~/Documents nor open log paths
# there — stage the wrapper and logs outside (memory: feedback_pool_wrapper_tcc).
STAGE_DIR="$HOME/.sutando/bin"
LOG_DIR="$HOME/Library/Application Support/Sutando/logs"
mkdir -p "$STAGE_DIR" "$LOG_DIR"
cp "$REPO_DIR/scripts/pool-core-wrapper.sh" "$STAGE_DIR/pool-core-wrapper.sh"
cp "$REPO_DIR/scripts/pool-follower-beat.sh" "$STAGE_DIR/pool-follower-beat.sh"
chmod +x "$STAGE_DIR/pool-core-wrapper.sh" "$STAGE_DIR/pool-follower-beat.sh"

# Resolve claude + python3 binaries. Caller's $PATH may not include the
# install dirs on launchd-spawned processes, so capture absolute paths now.
CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "error: 'claude' CLI not found on \$PATH" >&2
  exit 1
fi

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS"
UID_VAL="$(id -u)"
DOMAIN="gui/$UID_VAL"

# launchctl bootout is async — the service can still be in the "unloading"
# state when bootstrap fires, surfacing as "Bootstrap failed: 5: Input/output
# error". Retry bootstrap a few times with backoff before giving up. Observed
# 2026-05-19 on a 3-core re-install after the first slot's bootout.
bootstrap_with_retry() {
  local plist="$1"
  local i
  for i in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  # Final attempt with stderr visible so the script aborts with diagnostic.
  launchctl bootstrap "$DOMAIN" "$plist"
}

# Regression guard: Phase 2a ships the launchd plumbing + claim primitive,
# but NOT the `/proactive-loop-pool` skill. If we install plists that invoke
# a non-existent skill, launchd's KeepAlive will restart the failing claude
# process every ThrottleInterval seconds and burn quota. Refuse the install
# unless the skill is on disk OR the caller explicitly passes --force.
SKILL_DIR_CANDIDATES=(
  "$HOME/.claude/skills/proactive-loop-pool"
  "$HOME/.claude/projects/-Users-qingyunwu-Documents-github-sutando/skills/proactive-loop-pool"
)
SKILL_FOUND=0
for d in "${SKILL_DIR_CANDIDATES[@]}"; do
  if [ -d "$d" ]; then SKILL_FOUND=1; break; fi
done

FORCE=0
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=1
done

if [ "$SKILL_FOUND" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
  cat >&2 <<MSG
error: '/proactive-loop-pool' skill not found at:
$(for d in "${SKILL_DIR_CANDIDATES[@]}"; do echo "  $d"; done)

This is by design — Phase 2a ships the claim primitive + launchd shape, but
the pool-aware skill is Phase 2b. Installing now would spawn launchd jobs
that loop-fail on missing skill and burn quota.

To bypass and install anyway (e.g. for plist-shape testing), re-run with:
  bash scripts/install-core-pool.sh $N --force
MSG
  exit 1
fi

# Stop any pre-existing pool members beyond N so we shrink cleanly when
# the user runs `install-core-pool.sh 2` after a prior `install-core-pool.sh 5`.
#
# CRITICAL: glob is `com.sutando.core-[0-9]*.plist`, NOT `com.sutando.core-*.plist`.
# The narrower pattern intentionally excludes `com.sutando.core-agent.plist`
# (the pre-existing Sutando.app-managed agent) — removing that would tear
# down the menu-bar app's agent liaison. Regression caught 2026-05-18 23:14
# PT during script self-test; the recovery was `cp .plist.bak .plist &&
# launchctl bootstrap`. Don't loosen this glob.
shopt -s nullglob
for existing in "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do
  base="$(basename "$existing")"
  # Two flavors land in this glob:
  #   com.sutando.core-<N>.plist           — the claude session
  #   com.sutando.core-<N>-heartbeat.plist — its heartbeat sidecar
  # Extract the index from either and check shrink-bound.
  stem="${base#com.sutando.core-}"
  stem="${stem%.plist}"          # e.g. "3" or "3-heartbeat"
  idx="${stem%-heartbeat}"       # "3" in both cases
  if ! [[ "$idx" =~ ^[0-9]+$ ]]; then continue; fi
  if [ "$idx" -gt "$N" ]; then
    label="${base%.plist}"
    echo "removing stale pool member: $base"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$existing"
  fi
done
shopt -u nullglob

# Generate / refresh N plists. Bootout-then-bootstrap so a re-install picks
# up changes to env / paths / command.
for i in $(seq 1 "$N"); do
  PLIST="$LAUNCH_AGENTS/com.sutando.core-$i.plist"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sutando.core-$i</string>
  <key>ProgramArguments</key>
  <array>
    <string>$STAGE_DIR/pool-core-wrapper.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SUTANDO_CORE_ID</key><string>$i</string>
    <key>SUTANDO_CORE_POOL_SIZE</key><string>$N</string>
    <key>POOL_REPO_DIR</key><string>$REPO_DIR</string>
    <key>POOL_CLAUDE_BIN</key><string>$CLAUDE_BIN</string>
    <key>POOL_WORKSPACE</key><string>$WORKSPACE</string>
    <key>CLAUDE_CONFIG_DIR</key><string>$CLAUDE_CONFIG_DIR_EFFECTIVE</string>
    <key>PATH</key><string>$POOL_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/core-$i.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/core-$i.err</string>
</dict>
</plist>
PLIST_EOF
  # bootout first (idempotent — succeeds if not loaded) then bootstrap.
  launchctl bootout "$DOMAIN/com.sutando.core-$i" 2>/dev/null || true
  bootstrap_with_retry "$PLIST"
  echo "installed: com.sutando.core-$i (workspace=$WORKSPACE)"

  # Retired heartbeat sidecar: it ran `core_heartbeat.py`, which ignores
  # SUTANDO_CORE_ID (writes `<hostlabel>.alive`) and gates on a tmux pane a
  # `--print` follower never has — so `core-$i.alive` was never written and
  # the lead saw zero followers. The wrapper now owns the beat
  # (pool-follower-beat.sh, pid-bound to the claude child). Remove any
  # sidecar left by a previous install.
  HEART_PLIST="$LAUNCH_AGENTS/com.sutando.core-$i-heartbeat.plist"
  launchctl bootout "$DOMAIN/com.sutando.core-$i-heartbeat" 2>/dev/null || true
  rm -f "$HEART_PLIST"
done

echo
echo "Installed pool of $N core(s). Logs: $WORKSPACE/logs/core-{1..$N}.log"
echo
echo "IMPORTANT: This PR ships the launchd plumbing + claim primitive."
echo "The pool-aware skill '/proactive-loop-pool' is NOT in this PR (Phase 2b)."
echo "If you install with N>1 before the pool-aware skill is in place, you"
echo "will get duplicate task processing. Stay at N=1 until Phase 2b lands,"
echo "or test the claim primitive directly via tests/claim-task.test.py."
