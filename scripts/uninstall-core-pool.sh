#!/bin/bash
# Deprecated name (one release): the pool's workers are worker-N, so this script is now uninstall-worker-pool.sh.
echo "uninstall-core-pool.sh: deprecated name — use scripts/uninstall-worker-pool.sh" >&2
exec bash "$(dirname "${BASH_SOURCE[0]}")/uninstall-worker-pool.sh" "$@"
