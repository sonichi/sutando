#!/usr/bin/env bash
# _snapshot_per_host_config must never clobber a NEWER hosts/<label>/build_log.md
# with the workspace-root copy (the 2026-08-20 per-host log "eraser").
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
export WORKSPACE_DIR="$SB/ws"
mkdir -p "$WORKSPACE_DIR/hosts/testhost"
# Stub SCRIPT_PARENT so the function's config-dir resolution succeeds.
export SCRIPT_PARENT="$SB/parent"
mkdir -p "$SCRIPT_PARENT/scripts" "$SB/cfg"
printf '#!/bin/sh\necho "%s"\n' "$SB/cfg" > "$SCRIPT_PARENT/scripts/sutando-config.sh"
chmod +x "$SCRIPT_PARENT/scripts/sutando-config.sh"

# Load ONLY the function under test, plus a _host stub.
_host() { echo testhost; }
eval "$(sed -n '/^_snapshot_per_host_config() {/,/^}$/p' "$REPO/scripts/sync-workspace.sh")"

echo "1. stale root must NOT clobber a newer per-host log"
echo "stale relic" > "$WORKSPACE_DIR/build_log.md"
touch -t 202501010000 "$WORKSPACE_DIR/build_log.md"
echo "live per-host entries" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "newer per-host content survives the snapshot" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "live per-host entries" ]'

echo "2. newer root still snapshots over an older per-host copy"
echo "fresh root entries" > "$WORKSPACE_DIR/build_log.md"
touch -t 202501010000 "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "snapshot model preserved when root is the live writer" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "fresh root entries" ]'

echo "3. absent per-host copy is seeded from root"
rm "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "first snapshot creates the per-host copy" \
      '[ -f "$WORKSPACE_DIR/hosts/testhost/build_log.md" ]'

echo "4. absent root is a no-op"
rm "$WORKSPACE_DIR/build_log.md"
echo "keep me" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "no root file leaves the per-host log untouched" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "keep me" ]'

[ "$fails" -eq 0 ] && { echo "ALL PASS"; exit 0; }
echo "$fails FAILURE(S)"; exit 1
