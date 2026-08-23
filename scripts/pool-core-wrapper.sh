#!/bin/bash
# Staged to ~/.sutando/bin at install: launchd's TCC blocks shebang-exec on
# scripts under ~/Documents, so ProgramArguments must point OUTSIDE it.
# Userland cd into the repo is fine once the consented claude binary runs.
set -u
cd "$POOL_REPO_DIR" || exit 78
exec "$POOL_CLAUDE_BIN" --dangerously-skip-permissions \
  --add-dir "$POOL_WORKSPACE" --print "/proactive-loop-pool"
