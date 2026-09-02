#!/usr/bin/env bash
# Regenerate AGENTS.md from CLAUDE.md via systematic substitutions.
#
# AGENTS.md is the Codex/OpenAI-agent variant of CLAUDE.md. It is
# GENERATED — edit CLAUDE.md, then re-run this script. Commit both files.
#
# Substitutions applied (order matters: longer-match-first):
#   1. "Claude Code default" → "Codex default"
#   2. "Claude Code"         → "Codex"
#   3. "pgrep -f claude"     → "pgrep -f codex"
#   4. "CLAUDE.md"           → "AGENTS.md"
#
# Usage:
#   bash scripts/agents-md-sync.sh           # regenerate AGENTS.md
#   bash scripts/agents-md-sync.sh --check   # diff only, exit 1 if stale
#
# Closes #1455.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
src="$REPO_ROOT/CLAUDE.md"
dst="$REPO_ROOT/AGENTS.md"

[ -f "$src" ] || { echo "agents-md-sync: CLAUDE.md not found at $src" >&2; exit 1; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# PERSONAL_CLAUDE.md is a REAL FILENAME on disk, not a reference to this doc, so
# it must survive the CLAUDE.md -> AGENTS.md rename. Without the guard below the
# final substitution rewrote it to PERSONAL_AGENTS.md and AGENTS.md instructed
# the agent to read a file that nothing writes, nothing reads, and for which the
# repo ships no example — src/personal-claude-compact-hint.sh resolves
# personal_path("PERSONAL_CLAUDE.md"), and the template is PERSONAL_CLAUDE.md.example.
#
# Done by placeholder rather than a word-boundary match because `\b` is GNU sed
# only — this script has to run on the BSD sed that ships with macOS.
sed \
  -e 's/PERSONAL_CLAUDE\.md/@@SUTANDO_PERSONAL_DOC@@/g' \
  -e 's/ (Claude Code default)//g' \
  -e 's/Claude Code/Codex/g' \
  -e 's/pgrep -f claude/pgrep -f codex/g' \
  -e 's/CLAUDE\.md/AGENTS.md/g' \
  -e 's/@@SUTANDO_PERSONAL_DOC@@/PERSONAL_CLAUDE.md/g' \
  "$src" > "$tmp"

# Regression guard: verify expected markers are present in output.
for marker in 'Codex' 'AGENTS.md'; do
  grep -qF "$marker" "$tmp" || {
    echo "agents-md-sync: expected marker '$marker' missing from generated output" >&2
    exit 1
  }
done

# Context-budget ceiling: the runtime truncates/compacts past ~40k, and the
# file regrew ~780 B/day through Aug 2026 — gate it so it cannot re-cross.
BUDGET=40960
for f in "$src" "$tmp"; do
  size=$(wc -c < "$f")
  if [ "$size" -gt "$BUDGET" ]; then
    echo "agents-md-sync: $(basename "$f" .tmp) is ${size} bytes — over the ${BUDGET}-byte always-loaded budget. Relocate sections to docs/ (see #3016) instead of growing the file." >&2
    exit 1
  fi
done

if [ "$CHECK_ONLY" = "1" ]; then
  if diff -q "$dst" "$tmp" >/dev/null 2>&1; then
    echo "agents-md-sync: AGENTS.md is up to date"
    exit 0
  else
    echo "agents-md-sync: AGENTS.md is stale — run 'bash scripts/agents-md-sync.sh' to regenerate" >&2
    diff "$dst" "$tmp" | head -20 >&2 || true
    exit 1
  fi
fi

mv "$tmp" "$dst"
echo "agents-md-sync: AGENTS.md regenerated from CLAUDE.md ($(wc -l < "$dst") lines)"
