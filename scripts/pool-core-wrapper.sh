#!/bin/bash
# Staged to ~/.sutando/bin at install: launchd's TCC blocks shebang-exec on
# scripts under ~/Documents, so ProgramArguments must point OUTSIDE it.
# Userland cd into the repo is fine once the consented claude binary runs.
set -u
cd "$POOL_REPO_DIR" || exit 78
"$POOL_CLAUDE_BIN" --dangerously-skip-permissions \
  --add-dir "$POOL_WORKSPACE" --print "/proactive-loop-pool" &
CHILD=$!
# Liveness: the lead discovers followers via state/cores/core-N.alive; the
# beat is tied to the claude child's pid so it stops when the session does.
"$(dirname "$0")/pool-follower-beat.sh" \
  "core-${SUTANDO_CORE_ID}" "$POOL_WORKSPACE" "$CHILD" &
BEAT=$!
wait "$CHILD"
RC=$?
kill "$BEAT" 2>/dev/null
exit "$RC"
