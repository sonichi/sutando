#!/usr/bin/env bash
# usage: select-quarantined-suites.sh only|without <list> < discovered — only = the quarantined suites, without = the rest.
# Every entry is `<path> <issue url>`; a missing url, a duplicate or an undiscovered path exits 3.
set -euo pipefail
MODE="${1:-}"; LIST="${2:-}"
case "$MODE" in only|without) [ -f "$LIST" ] ;; *) false ;; esac || { echo "usage: $0 only|without <list> < discovered" >&2; exit 2; }
ALL="$(mktemp)"; WANT="$(mktemp)"; trap 'rm -f "$ALL" "$WANT"' EXIT
cat > "$ALL"
bad="$(grep -vE '^(#|$)' "$LIST" | awk 'NF != 2 || $2 !~ /^https:\/\/github\.com\/sonichi\/sutando\/issues\/[0-9]+$/' || true)"
[ -z "$bad" ] || { printf 'every entry in %s is <path> <issue url>:\n%s\n' "$LIST" "$bad" >&2; exit 3; }
grep -vE '^(#|$)' "$LIST" | awk '{print $1}' > "$WANT" || true
dup="$(sort "$WANT" | uniq -d)"
[ -z "$dup" ] || { printf 'quarantined twice in %s:\n%s\n' "$LIST" "$dup" >&2; exit 3; }
stale="$(grep -vxF -f "$ALL" "$WANT" || true)"
[ -z "$stale" ] || { printf 'quarantined in %s but not discovered:\n%s\n' "$LIST" "$stale" >&2; exit 3; }
if [ "$MODE" = only ]; then
  grep -xF -f "$WANT" "$ALL" || true
else
  grep -vxF -f "$WANT" "$ALL" || true
fi
