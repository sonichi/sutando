#!/usr/bin/env bash
# Build the pointer-overlay CLI that renders the `point_at` marker.
# Idempotent — safe to re-run. Produces ./pointer-overlay in this dir.
#
# The binary stays in this skill directory, mirroring context-drop/ax-read, so a
# public install finds it without extra config.
#
# Run it against the path the production tool actually writes:
#   SUTANDO_POINTER_CMD="$(bash scripts/sutando-config.sh workspace)/state/pointer-cmd.json" \
#     ./pointer-overlay &

set -euo pipefail
cd "$(dirname "$0")"

swift build -c release --product pointer-overlay
cp .build/release/pointer-overlay ./pointer-overlay

echo "✓ pointer-overlay built at $(pwd)/pointer-overlay"
