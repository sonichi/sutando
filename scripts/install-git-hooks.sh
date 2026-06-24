#!/usr/bin/env bash
# Sutando git hooks installer.
#
# Wires `.githooks/` (tracked in the repo) as the active hooks directory for
# this clone via `git config core.hooksPath .githooks`. This is the standard
# "tracked hooks + per-clone opt-in" pattern — keeps the hooks shared in the
# repo while letting individual users disable them if needed (`git config
# --unset core.hooksPath`).
#
# Idempotent: re-running prints "already installed" without changing anything.
#
# Run once after cloning:
#     bash scripts/install-git-hooks.sh
#
# Or fold into `bash src/startup.sh` if you want it on every boot.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

if [[ ! -d ".githooks" ]]; then
  echo "✖ .githooks/ directory not found at $REPO_ROOT" >&2
  echo "  Are you in a Sutando checkout?" >&2
  exit 1
fi

current="$(git config --get core.hooksPath 2>/dev/null || true)"

if [[ "$current" == ".githooks" ]]; then
  echo "✓ git hooks already installed (core.hooksPath = .githooks)"
  exit 0
fi

if [[ -n "$current" && "$current" != ".githooks" ]]; then
  echo "⚠ git config core.hooksPath is currently '$current' (custom)." >&2
  echo "  Overwriting with .githooks. Your previous setting is lost — re-set" >&2
  echo "  it manually after if you need to chain." >&2
fi

git config core.hooksPath .githooks
echo "✓ Installed Sutando git hooks (core.hooksPath -> .githooks)"
echo ""
echo "  Hooks active for this clone:"
ls -1 .githooks/ | sed 's/^/    • /'
echo ""
echo "  To disable later: git config --unset core.hooksPath"
