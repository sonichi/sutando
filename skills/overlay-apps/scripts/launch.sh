#!/bin/bash
# Deploy + launch the Sutando overlay applications.
#
# Workspace contract: the app *source of truth* is skills/overlay-apps/app/ in
# the repo; the *running instance* (with node_modules + any local state) lives
# under the Sutando workspace at $SUTANDO_WORKSPACE/overlay-apps/. This script
# syncs source → workspace, installs deps, and starts the app.

set -e

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SRC="$SKILL_DIR/app"
REPO_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
# Workspace resolution via the canonical M0 helper.
WORKSPACE="$(bash "$REPO_ROOT/scripts/sutando-config.sh" workspace)"
APP_DIR="$WORKSPACE/overlay-apps/benchmark-overlay"

mkdir -p "$APP_DIR"

# Sync source into the workspace instance, preserving its node_modules.
rsync -a --delete \
  --exclude node_modules \
  --exclude .gitignore \
  "$APP_SRC/" "$APP_DIR/"

cd "$APP_DIR"

# (Re)install dependencies when missing or when the lockfile changed.
if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
  echo "overlay-apps: installing dependencies in $APP_DIR ..."
  npm install --no-audit --no-fund
fi

echo "overlay-apps: launching from $APP_DIR"
exec npm start
