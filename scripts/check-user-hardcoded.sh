#!/usr/bin/env bash
# Audit code for owner-specific literals that shouldn't ship in a shareable repo.
#
# Repo is meant to be 100% shareable — any clone+install should run. Personal
# data (path, phone, Discord/Slack IDs, hostname) must stream in from .env /
# workspace / memory, never be baked into source code.
#
# This script greps src/, skills/*/scripts/, and scripts/ for known owner
# literals and exits non-zero if any are found.
#
# Run pre-merge:  bash scripts/check-user-hardcoded.sh
# Exit 0 = clean; 1 = hits found.

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Only cd to repo if caller hasn't overridden search roots (test mode runs
# against synthetic dirs in $PWD).
if [ -z "${CHECK_USER_HARDCODED_ROOTS:-}" ]; then
  cd "$REPO"
fi

PATTERNS=(
  '/Users/qingyunwu'
  '/Users/bassilkhilo'
  '\+14344664925'
  '1025828152183885925'
  '1504379900050673747'
  'U081446333M'
  'Qingyuns-MacBook-Pro'
)

INCLUDE_GLOBS=(
  --include='*.py'
  --include='*.ts'
  --include='*.tsx'
  --include='*.js'
  --include='*.sh'
  --include='*.swift'
)

EXCLUDE_PATHS=(
  --exclude-dir=.git
  --exclude-dir=node_modules
  --exclude-dir=__pycache__
  --exclude-dir=.pytest_cache
  --exclude-dir=tests
  --exclude-dir=notes
  --exclude-dir=docs
)

# Search roots — override via CHECK_USER_HARDCODED_ROOTS=":dir1:dir2" for testing.
if [ -n "${CHECK_USER_HARDCODED_ROOTS:-}" ]; then
  IFS=':' read -r -a SEARCH_ROOTS <<< "${CHECK_USER_HARDCODED_ROOTS#:}"
else
  SEARCH_ROOTS=(src skills scripts)
fi
SELF="scripts/check-user-hardcoded.sh"

hits=0
report=""
for pat in "${PATTERNS[@]}"; do
  raw=$(grep -RnE "${INCLUDE_GLOBS[@]}" "${EXCLUDE_PATHS[@]}" -- "$pat" "${SEARCH_ROOTS[@]}" 2>/dev/null || true)
  if [ -z "$raw" ]; then
    continue
  fi
  filtered=""
  while IFS= read -r line; do
    case "$line" in
      "$SELF":*) continue ;;
    esac
    filtered+="$line"$'\n'
    hits=$((hits + 1))
  done <<< "$raw"
  if [ -n "$filtered" ]; then
    report+="=== pattern: $pat"$'\n'
    report+="$filtered"$'\n'
  fi
done

if [ $hits -eq 0 ]; then
  echo "✓ no owner-hardcoded literals in src/ skills/ scripts/"
  exit 0
fi

echo "✗ $hits owner-hardcoded literal(s) found in shareable code:"
echo
echo "$report"
echo "Replace literals with env vars / workspace yaml / memory lookups."
echo "See memory: user-data-and-logic-belong-in-workspace (feedback_user_config_in_workspace.md)."
exit 1
