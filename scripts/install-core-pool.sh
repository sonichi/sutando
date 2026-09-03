#!/bin/bash
# Deprecated name (one release): the pool's workers are worker-N, so this script is now install-worker-pool.sh.
echo "install-core-pool.sh: deprecated name — use scripts/install-worker-pool.sh" >&2
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-worker-pool.sh" "$@"
