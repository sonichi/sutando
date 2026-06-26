#!/usr/bin/env bash
# Sutando lint: forbid inline `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` in shell.
#
# Bash callers MUST resolve Claude Code's per-user home via the M0 helper:
#
#   $(bash scripts/sutando-config.sh claude-home-path [subpath...])
#
# instead of the inline `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` shorthand.
# Two reasons:
#
#   1. The helper centralizes the resolution order ($CLAUDE_CONFIG_DIR →
#      $CLAUDE_HOME → ~/.claude/), mirroring src/util_paths.py:claude_home_path.
#   2. The helper emits the deprecation-on-fallback banner (#1534) when
#      neither CCD nor CLAUDE_HOME is set — the inline form silently falls
#      back to ~/.claude/, defeating the visibility #1534 was designed to add.
#
# This lint catches new inline uses on ADDED lines (PR diff). Existing
# legacy offenders are migrated as part of the sweep that introduces this
# lint; future contributors get a CI failure if they reintroduce the
# pattern. Tracking issue / motivation: owner directive 2026-06-07
# (`why we still have /.claude/?` DM, response at task-1780871881546).
#
# Allowed files (the helper itself + comments documenting the anti-pattern):
#   scripts/sutando-config.sh
#   src/startup.sh
#   scripts/lint-claude-home-path.sh  (this file)
#
# Companion lint for the WRITE-FROM source path (SOURCE_CLAUDE_CONFIG_DIR)
# is OUT OF SCOPE here — that env var has different semantics (migration
# read-side, per src/util_paths.py docstring) and the matching pattern is
# explicitly allowed.
#
# Usage:
#   bash scripts/lint-claude-home-path.sh          # scan whole tree
#   bash scripts/lint-claude-home-path.sh --diff   # scan only added/modified lines vs BASE_REF

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

mode="${1:-all}"

# Pattern: matches both `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` and the rare
# inverted ordering. Excludes SOURCE_CLAUDE_CONFIG_DIR (different concept).
PATTERN_INLINE='\$\{CLAUDE_CONFIG_DIR:-\$HOME/\.claude\}'

# Allowed files — may reference the anti-pattern in comments / docstrings.
ALLOWED='^(scripts/sutando-config\.sh|src/startup\.sh|scripts/lint-claude-home-path\.sh|tests/[^/]+\.(test\.)?sh)$'

if [[ "$mode" == "--diff" ]]; then
  base="${BASE_REF:-origin/main}"
  files="$(git diff --name-only --diff-filter=AM "$base"...HEAD -- '*.sh' '*.bash')"
else
  files="$(git ls-files -- '*.sh' '*.bash')"
fi

if [[ -z "$files" ]]; then
  echo "lint-claude-home-path: nothing to scan (mode=$mode)"
  exit 0
fi

found=0
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  if [[ "$f" =~ $ALLOWED ]]; then
    continue
  fi
  if [[ "$mode" == "--diff" ]]; then
    # Only flag lines ADDED in the diff (not surrounding context).
    # git diff prefix: '+' (added), ' ' (context), '-' (removed).
    added="$(git diff "$base"...HEAD -- "$f" | awk '/^\+[^+]/ {print substr($0, 2)}')"
    matches="$(echo "$added" | grep -E "$PATTERN_INLINE" || true)"
  else
    matches="$(grep -E "$PATTERN_INLINE" "$f" || true)"
  fi
  if [[ -n "$matches" ]]; then
    found=1
    echo "lint-claude-home-path: forbidden inline pattern in $f"
    while IFS= read -r line; do
      echo "  $line"
    done <<< "$matches"
  fi
done <<< "$files"

if [[ "$found" -eq 1 ]]; then
  cat >&2 <<'EOF'

ERROR: One or more files use the inline `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`
pattern. This bypasses the M0 helper + #1534 deprecation banner.

Fix: replace each occurrence with the helper. Examples:

  Before:
    _CHAN_BASE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/channels"

  After:
    _CHAN_BASE="$(bash "$REPO_DIR/scripts/sutando-config.sh" claude-home-path channels)"

  Before:
    PROXY="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/quota-tracker/scripts/credential-proxy.ts"

  After:
    PROXY="$(bash "$REPO_DIR/scripts/sutando-config.sh" claude-home-path skills/quota-tracker/scripts/credential-proxy.ts)"

See `scripts/sutando-config.sh` for the helper's resolution order and the
`SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1` opt-out.
EOF
  exit 1
fi

echo "lint-claude-home-path: clean (mode=$mode, scanned $(wc -l <<< "$files" | tr -d ' ') files)"
exit 0
