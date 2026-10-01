#!/usr/bin/env bash
# Splits the discovered suite list (stdin) against the load-sensitive list: `only`
# prints the listed suites, `without` prints every other one. An entry discovery
# does not have, or one listed twice, is an error: silently dropping it would send
# that suite back to the shared legs, which is what the list exists to prevent.
# usage: select-load-sensitive-suites.sh only|without <list> < discovered
set -euo pipefail
MODE="${1:-}"; LIST="${2:-}"
case "$MODE" in only|without) [ -n "$LIST" ] ;; *) false ;; esac || { echo "usage: $0 only|without <list> < discovered" >&2; exit 2; }
ALL="$(mktemp)"; WANT="$(mktemp)"; trap 'rm -f "$ALL" "$WANT"' EXIT
cat > "$ALL"
grep -vE '^(#|$)' "$LIST" > "$WANT" || true
dup="$(sort "$WANT" | uniq -d)"
[ -z "$dup" ] || { printf 'listed twice in %s:\n%s\n' "$LIST" "$dup" >&2; exit 3; }
stale="$(grep -vxF -f "$ALL" "$WANT" || true)"
[ -z "$stale" ] || { printf 'listed in %s but not discovered:\n%s\n' "$LIST" "$stale" >&2; exit 3; }
if [ "$MODE" = only ]; then
  # No match at all is an empty leg, which the caller must not run as a pass.
  grep -xF -f "$WANT" "$ALL"
else
  grep -vxF -f "$WANT" "$ALL" || true
fi
