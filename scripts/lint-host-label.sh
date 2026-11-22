#!/usr/bin/env bash
# Sutando lint: forbid raw hostname-slug derivation in AGENT-FACING instructions.
#
# The per-host directory slug (`hosts/<label>/…` — pending-questions.md,
# crons.json, recap.json, PERSONAL_CLAUDE.md, state/cores/<label>.alive) MUST be
# resolved via the single source of truth:
#
#     bash scripts/sutando-config.sh host-label
#     # → src/util_paths.py:_host_label():  $SUTANDO_HOST_LABEL > scutil LocalHostName > short hostname
#
# The drift-prone recipe `hostname | sed 's/\..*//'` returns the DHCP/network
# short name (e.g. `QingyunsMBP2200`) which diverges from the stable Bonjour
# `LocalHostName` (`Qingyuns-MacBook-Pro-2200`) on macOS. An agent that follows
# a doc telling it to compute the slug that way writes per-host files into a
# ghost `hosts/<wrong-label>/` dir the readers never consult — silent per-host
# data loss (#1745, fixed for code in #1745/#1771, for docs in #2136).
#
# This lint keeps the docs from regressing. It is agent-facing-files-only by
# design: legitimate CODE fallbacks (scutil-first, then `hostname | sed` on
# Linux) are NOT flagged.
#
# Usage:
#   bash scripts/lint-host-label.sh          # scan whole tree (local: see all debt)
#   bash scripts/lint-host-label.sh --diff   # scan only added lines vs base (CI)
#
# The CI workflow `.github/workflows/lint-host-label.yml` calls this with
# --diff so pre-existing lines stay un-flagged; only NEW reintroductions fail.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

mode="${1:-all}"  # all | --diff

# The forbidden recipe: bare `hostname | sed …` used to DERIVE the slug.
PATTERN='hostname[[:space:]]*\|[[:space:]]*sed'

# Only these files are agent-facing instructions where an agent might copy a
# recipe verbatim. Code lives elsewhere and keeps its legitimate fallbacks.
AGENT_FACING_GLOBS=(CLAUDE.md 'docs/*.md' 'docs/**/*.md' 'skills/**/*.md' 'skills/**/*.SKILL.md' 'PERSONAL_CLAUDE.md')

# A matching line is ALLOWED (not a regression) when it is teaching AGAINST the
# recipe or pointing at the correct resolver on the same line — i.e. the line
# also mentions one of these. This lets the #1745 rationale and the explicit
# "Do NOT use `hostname | sed`" warning stay without tripping the guard.
ALLOW_ON_LINE='host-label|scutil|LocalHostName|Do NOT|do not use|don.t use|#1745|drift|WRONG|ghost'

emit() { printf '%s\n' "$1" >&2; }

is_instructional() {
  # AGENT-FACING = markdown instructions an agent may copy verbatim.
  # Only CLAUDE.md / PERSONAL_CLAUDE.md / *.md under docs|skills. Code files
  # (.sh/.py/.ts) are intentionally NOT scanned — they keep legitimate
  # fallbacks (e.g. self-diagnose/gather.sh's scutil-first, hostname-on-Linux).
  case "$1" in
    CLAUDE.md|PERSONAL_CLAUDE.md) return 0 ;;
    docs/*.md|skills/*.md) return 0 ;;
    docs/*/*.md|docs/*/*/*.md|skills/*/*.md|skills/*/*/*.md|skills/*/*/*/*.md) return 0 ;;
    *) return 1 ;;
  esac
}

collect_candidate_lines() {
  # Prints "path:line:content" for forbidden matches in agent-facing markdown.
  if [ "$mode" = "--diff" ]; then
    local base="${BASE_REF:-HEAD~1}"
    # Added lines only (leading '+', not the +++ header), file+line via a tiny
    # awk state machine over git diff's unified output.
    git diff --unified=0 "$base"...HEAD -- CLAUDE.md PERSONAL_CLAUDE.md docs skills 2>/dev/null \
      | awk '
          /^\+\+\+ b\// { f=substr($0,7); next }
          /^@@/ { if (match($0, /\+[0-9]+/)) { ln=substr($0,RSTART+1,RLENGTH-1)+0 } next }
          /^\+/ { print f ":" ln ":" substr($0,2); ln++ ; next }
          /^-/  { next }
          { ln++ }
        '
  else
    # Whole-tree scan.
    git grep -nE "$PATTERN" -- CLAUDE.md PERSONAL_CLAUDE.md docs skills 2>/dev/null
  fi
}

violations=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  path="${line%%:*}"
  content="${line#*:*:}"
  # Only agent-facing markdown (code files keep legit fallbacks)...
  is_instructional "$path" || continue
  # ...must contain the forbidden recipe...
  echo "$content" | grep -qE "$PATTERN" || continue
  # ...and NOT be a warning / correct-resolver line.
  if echo "$content" | grep -qiE "$ALLOW_ON_LINE"; then
    continue
  fi
  emit "  ✗ ${line%%:*}: raw hostname-slug recipe — use: bash scripts/sutando-config.sh host-label"
  emit "      ${content}"
  violations=$((violations + 1))
done < <(collect_candidate_lines)

if [ "$violations" -gt 0 ]; then
  emit ""
  emit "host-label lint FAILED ($violations): agent-facing docs must not derive the per-host slug"
  emit "from a bare 'hostname | sed'. Use \`bash scripts/sutando-config.sh host-label\` (scutil-first,"
  emit "drift-immune; #1745). Legit CODE fallbacks (e.g. self-diagnose/gather.sh) are not scanned."
  exit 1
fi

echo "host-label lint OK — no raw hostname-slug recipes in agent-facing instructions."
