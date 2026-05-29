#!/usr/bin/env bash
# Smoke test for src/migrate.sh — runs the bundling phase and asserts that
# every payload the current main machine bundles actually lands inside the
# generated tarball.
#
# Why this exists: migrate.sh has had three regressions in three commits
# on PR #343 (self-rename mv, dotfile glob miss, duplicate-mv Invalid-argument).
# Each was a silent bug; the script exited 0 with the bundle half-empty or
# never tarred. This script fails loudly if any of those classes return.
#
# Usage:
#   bash scripts/test-migrate-bundle.sh          # run the full bundle + assertions
#
# Exits 0 on pass, 1 on any missing payload. Cleans up its own artifacts on
# success. On failure, prints the list of missing entries and leaves the
# tarball behind at ~/Desktop/sutando-migration.tar.gz for inspection.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAR="$HOME/Desktop/sutando-migration.tar.gz"

# Clean any leftover artifact from a previous run.
rm -rf "$HOME/Desktop/sutando-migration" "$TAR"

echo "=== Running src/migrate.sh ==="
# Run from /tmp so the session-handoff dump can't clobber anything in the
# repo while we're iterating.
(cd /tmp && bash "$REPO/src/migrate.sh") > /tmp/migrate-output.log 2>&1 || {
  echo "✗ migrate.sh exited non-zero. Last 30 lines:"
  tail -30 /tmp/migrate-output.log
  exit 1
}

if [ ! -f "$TAR" ]; then
  echo "✗ Tarball not found at $TAR"
  echo "  migrate.sh exited 0 but never wrote the tar. Inspect /tmp/migrate-output.log."
  exit 1
fi

echo "  ✓ tarball produced ($(du -h "$TAR" | cut -f1))"

# Build a manifest of archived entries once, reuse below.
MANIFEST=$(tar tzf "$TAR")

# Entries we require. Each entry is a regex matched against the tar manifest.
# Only assert the payloads that are actually expected to be present on the
# CURRENT machine — we look at the source file system to decide. This keeps
# the test portable between Mac Mini and MacBook without false failures.
REQUIRED=()
REQUIRED+=("^sutando-migration/\\.env$")
REQUIRED+=("^sutando-migration/memory/MEMORY\\.md$")
REQUIRED+=("^sutando-migration/setup-new-mac\\.sh$")

# Notes (PR #343 addition).
if [ -d "$REPO/notes" ] && [ "$(find "$REPO/notes" -name '*.md' | wc -l | tr -d ' ')" -gt 0 ]; then
  REQUIRED+=("^sutando-migration/notes/.*\\.md$")
fi

# ~/.claude.json (PR #343 addition — MCP registrations).
if [ -f "$HOME/.claude.json" ]; then
  REQUIRED+=("^sutando-migration/claude-config/claude\\.json$")
fi

# claude config basics.
if [ -d "$HOME/.claude" ]; then
  REQUIRED+=("^sutando-migration/claude-config/")
fi

# gws credentials — only assert the entries that actually exist on this host,
# because the bundle step only copies present files.
if [ -d "$HOME/.config/gws" ]; then
  for f in client_secret.json credentials.enc .encryption_key token_cache.json; do
    if [ -f "$HOME/.config/gws/$f" ]; then
      REQUIRED+=("^sutando-migration/gws/$(echo "$f" | sed 's/\./\\./g')$")
    fi
  done
fi

# Session history.
SESSION_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando"
if [ -d "$SESSION_DIR" ] && ls "$SESSION_DIR"/*.jsonl >/dev/null 2>&1; then
  REQUIRED+=("^sutando-migration/session/.*\\.jsonl$")
fi

echo ""
echo "=== Asserting required payloads ==="
MISSING=0
for pat in "${REQUIRED[@]}"; do
  if echo "$MANIFEST" | grep -Eq "$pat"; then
    echo "  ✓ $pat"
  else
    echo "  ✗ MISSING: $pat"
    MISSING=$((MISSING + 1))
  fi
done

# Bonus check: setup-new-mac.sh should parse as bash. Catches heredoc regressions.
echo ""
echo "=== Syntax-checking generated setup-new-mac.sh ==="
tmpdir=$(mktemp -d)
tar xzf "$TAR" -C "$tmpdir"
if bash -n "$tmpdir/sutando-migration/setup-new-mac.sh"; then
  echo "  ✓ setup-new-mac.sh parses"
else
  echo "  ✗ setup-new-mac.sh has a syntax error"
  MISSING=$((MISSING + 1))
fi
rm -rf "$tmpdir"

echo ""
if [ "$MISSING" -eq 0 ]; then
  echo "=== PASS ($((${#REQUIRED[@]})) required entries + setup-new-mac.sh parse) ==="
  rm -f "$TAR"
  exit 0
else
  echo "=== FAIL: $MISSING missing entries ==="
  echo "Tarball left at $TAR for inspection."
  exit 1
fi
