#!/usr/bin/env bash
# Migration 0001 — record SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR rename (#876).
#
# Most installs are already post-rename (the alias in workspace_default.py has been
# forwarding since #876). This script handles the edge case where a user's ~/.sutando/env
# still contains the old key and performs the in-place rename so it's clean.
# Idempotent: exits 0 with no mutation when the rename is already applied or neither
# key is set.
set -euo pipefail

ENV_FILE="${SUTANDO_ENV_FILE:-$HOME/.sutando/env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "[0001] No $ENV_FILE — nothing to rename."
  exit 0
fi

if ! grep -q "^SUTANDO_PRIVATE_DIR=" "$ENV_FILE" 2>/dev/null; then
  echo "[0001] SUTANDO_PRIVATE_DIR not found in $ENV_FILE — already migrated or never set."
  exit 0
fi

# In-place sed rename: SUTANDO_PRIVATE_DIR= → SUTANDO_MEMORY_DIR=
if sed -i '' 's/^SUTANDO_PRIVATE_DIR=/SUTANDO_MEMORY_DIR=/' "$ENV_FILE" 2>/dev/null ||
   sed -i  's/^SUTANDO_PRIVATE_DIR=/SUTANDO_MEMORY_DIR=/' "$ENV_FILE" 2>/dev/null; then
  echo "[0001] Renamed SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR in $ENV_FILE"
else
  echo "[0001] ERROR: sed rename failed" >&2
  exit 1
fi

# Verify post-state
if grep -q "^SUTANDO_PRIVATE_DIR=" "$ENV_FILE"; then
  echo "[0001] ERROR: SUTANDO_PRIVATE_DIR still present after rename" >&2
  exit 1
fi
echo "[0001] Verified: SUTANDO_PRIVATE_DIR absent, migration complete."
