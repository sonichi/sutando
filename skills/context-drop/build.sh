#!/usr/bin/env bash
# Build the public ax-read CLI used by Sutando.app's dropContext.
# Idempotent — safe to re-run. Produces ./ax-read in this dir.
#
# Sutando.app's resolveAxReadPath() looks for ax-read in this skill
# directory; the binary stays here rather than in src/ or a build output
# folder so the public install (`bash src/startup.sh`) finds it without
# extra config.

set -euo pipefail
cd "$(dirname "$0")"

swift build -c release --product ax-read
cp .build/release/ax-read ./ax-read

echo "✓ ax-read built at $(pwd)/ax-read"
