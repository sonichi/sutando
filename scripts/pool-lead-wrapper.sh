#!/bin/bash
# Staged into the install bin dir (see scripts/install-worker-pool.sh, which owns
# that path): launchd's TCC blocks exec of scripts
# under the Documents tree, so the plist's ProgramArguments must point outside it.
set -u

DAEMON="$POOL_REPO_DIR/scripts/pool-lead-daemon.py"

# One lead per install. A lead already running from this checkout (e.g. the
# unsupervised startup.sh fallback) keeps ownership; KeepAlive retries us after
# ThrottleInterval, so supervision resumes as soon as that one exits.
if pgrep -f "$DAEMON" > /dev/null 2>&1; then
  echo "pool-lead: a lead from this checkout is already running — standing down"
  sleep "${SUTANDO_POOL_LEAD_DEFER_S:-30}"
  exit 0
fi

exec "${POOL_PY:-python3}" "$DAEMON" "$@"
