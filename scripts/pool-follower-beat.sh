#!/bin/bash
# Follower liveness writer: beats <workspace>/state/cores/<instance>.alive
# while <watch-pid> is alive, then exits. Spawned by pool-core-wrapper.sh with
# the claude child's pid so the beat can never outlive the session it vouches
# for (the failure class core_heartbeat.py documents for its own 2026-08-01
# fix). No unlink on exit: KeepAlive restarts the follower within ~30s and the
# 90s stale window must absorb that gap, or the lead would reclaim in-flight
# assignments on every clean restart.
#
# Usage: pool-follower-beat.sh <instance> <workspace> <watch-pid>
set -u
INSTANCE="$1"
WORKSPACE="$2"
WATCH_PID="$3"
INTERVAL="${SUTANDO_POOL_BEAT_INTERVAL:-30}"

DIR="$WORKSPACE/state/cores"
FILE="$DIR/$INSTANCE.alive"

while kill -0 "$WATCH_PID" 2>/dev/null; do
  mkdir -p "$DIR"
  printf '{"role": "follower", "instance": "%s", "pid": %d, "ts": %d}\n' \
    "$INSTANCE" "$WATCH_PID" "$(date +%s)" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"
  sleep "$INTERVAL"
done
