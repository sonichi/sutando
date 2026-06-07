#!/usr/bin/env bash
# Generate AGENTS.md from CLAUDE.md via systematic substitutions.
# Re-runnable; output is reproducible from CLAUDE.md alone.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$REPO_ROOT/CLAUDE.md"
dst="$REPO_ROOT/AGENTS.md"
[ -f "$src" ] || { echo "CLAUDE.md missing" >&2; exit 1; }

sed \
  -e 's/Claude Code default/Codex default/g' \
  -e 's/Claude Code/Codex/g' \
  -e 's/pgrep -f claude/pgrep -f Codex/g' \
  -e 's/CLAUDE\.md/AGENTS.md/g' \
  -e 's/\.claude/\.codex/g' \
  "$src" > "$dst"

# Verify expected substitutions actually fired (regression catch)
for marker in 'Codex' 'AGENTS.md' '.codex'; do
  grep -qF "$marker" "$dst" || { echo "agents-md-sync: expected marker '$marker' missing from output" >&2; exit 1; }
done
echo "agents-md-sync: AGENTS.md regenerated from CLAUDE.md ($(wc -l < "$dst") lines)"
