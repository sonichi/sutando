#!/bin/bash
# Migrate ~/Library/LaunchAgents/com.sutando.*.plist StandardOutPath /
# StandardErrorPath entries from /Desktop/sutando/src/*.log to
# /Desktop/sutando/logs/*.log, matching PR #251's runtime-artifacts refactor.
#
# Runs idempotently: already-migrated plists are skipped. Unloads and reloads
# each affected service so the new path takes effect without dropping launchd's
# KeepAlive contract.
#
# Usage:
#   bash src/migrate-plists-to-logs-dir.sh              # migrate
#   bash src/migrate-plists-to-logs-dir.sh --dry-run    # report what would change
#
# Exit codes:
#   0 — success (including no-op on already-migrated installs)
#   1 — error (one or more plists failed to parse, unload, or reload)

set -e

WORKSPACE="${WORKSPACE:-$HOME/Desktop/sutando}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '3,17p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg"; exit 2 ;;
  esac
done

echo "Sutando plist migration (src/*.log → logs/*.log)"
echo "Workspace: $WORKSPACE"
[ "$DRY_RUN" = "1" ] && echo "DRY RUN — no changes will be written"
echo ""

mkdir -p "$WORKSPACE/logs" "$WORKSPACE/state"

migrated=0
skipped=0
errors=0

# Iterate every com.sutando.*.plist currently in LaunchAgents
for plist in "$LAUNCH_AGENTS"/com.sutando.*.plist; do
  [ -f "$plist" ] || continue
  name=$(basename "$plist" .plist)

  # Does this plist still reference the old path?
  if ! grep -q "$WORKSPACE/src/[a-zA-Z0-9_-]*\.log" "$plist" 2>/dev/null; then
    echo "  ✓ $name (already migrated)"
    skipped=$((skipped + 1))
    continue
  fi

  # Show the current log paths
  old_paths=$(grep -o "$WORKSPACE/src/[a-zA-Z0-9_-]*\.log" "$plist" | sort -u)
  for p in $old_paths; do
    new_path=$(echo "$p" | sed "s|$WORKSPACE/src/|$WORKSPACE/logs/|")
    echo "  → $name: $p → $new_path"
  done

  if [ "$DRY_RUN" = "1" ]; then
    continue
  fi

  # Rewrite in place. sed -i '' on macOS
  if ! sed -i '' "s|$WORKSPACE/src/\\([a-zA-Z0-9_-]*\\)\\.log|$WORKSPACE/logs/\\1.log|g" "$plist"; then
    echo "    ✗ sed failed on $plist"
    errors=$((errors + 1))
    continue
  fi

  # Validate the plist still parses
  if ! plutil -lint "$plist" >/dev/null 2>&1; then
    echo "    ✗ plutil -lint failed on $plist after rewrite — please restore manually"
    errors=$((errors + 1))
    continue
  fi

  # Reload the service so launchd picks up the new StandardOutPath
  if launchctl list | grep -q "^[0-9-]*[[:space:]][0-9]*[[:space:]]$name$"; then
    launchctl unload "$plist" 2>/dev/null || true
    if ! launchctl load "$plist" 2>/dev/null; then
      echo "    ✗ launchctl load failed for $name"
      errors=$((errors + 1))
      continue
    fi
  fi

  migrated=$((migrated + 1))
done

echo ""
echo "Summary: $migrated migrated, $skipped already-migrated, $errors errors"

if [ "$DRY_RUN" = "1" ]; then
  echo "(dry run — nothing was changed)"
fi

# Drop stale log files left behind in src/ (only when not dry-run and the
# migration actually ran). They would otherwise confuse future audits.
if [ "$DRY_RUN" = "0" ] && [ "$migrated" -gt 0 ]; then
  echo ""
  echo "Cleaning up stale src/*.log files (safe — new writes land in logs/)..."
  for stale_log in "$WORKSPACE"/src/*.log; do
    [ -f "$stale_log" ] || continue
    # Don't delete a log that's still being written (mtime within 60s)
    if [ $(( $(date +%s) - $(stat -f %m "$stale_log") )) -lt 60 ]; then
      echo "  skip (recently written): $(basename "$stale_log")"
      continue
    fi
    rm "$stale_log"
    echo "  removed: $(basename "$stale_log")"
  done
fi

exit $errors
