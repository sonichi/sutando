#!/bin/bash
# Deprecated name (one release): the pool's workers are worker-N, so this script is now pool-worker-wrapper.sh.
echo "pool-core-wrapper.sh: deprecated name — use scripts/pool-worker-wrapper.sh" >&2
exec bash "$(dirname "${BASH_SOURCE[0]}")/pool-worker-wrapper.sh" "$@"
