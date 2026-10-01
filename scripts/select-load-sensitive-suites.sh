#!/usr/bin/env bash
# usage: select-load-sensitive-suites.sh only|without <list> <receipt-dir> < discovered | verify only|without <list> <receipt-dir> <selected> <to-run>
# Stale/duplicate entry: exit 3. Selecting writes a receipt; verify exits 4 unless <to-run> is what this script selected.
set -euo pipefail
MODE="${1:-}"
usage() { echo "usage: $0 only|without <list> <receipt-dir> < discovered | verify only|without <list> <receipt-dir> <selected> <to-run>" >&2; exit 2; }
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

if [ "$MODE" = verify ]; then
  [ "$#" -eq 6 ] || usage
  KIND="$2"; LIST="$3"; RCPT="$4/selector.$2.receipt"; SELECTED="$5"; RUN="$6"
  case "$KIND" in only|without) ;; *) usage ;; esac
  fail() { echo "selector receipt check ($KIND): $*" >&2; exit 4; }
  [ -f "$RCPT" ] || fail "no receipt at $RCPT — the shared selector did not produce this leg's list"
  [ "$(sed -n 's/^list_sha256=//p' "$RCPT")" = "$(sha "$LIST")" ] || fail "$LIST changed after selection"
  [ "$(sed -n 's/^output_sha256=//p' "$RCPT")" = "$(sha "$SELECTED")" ] || fail "$SELECTED is not what the selector emitted"
  if [ "$KIND" = only ]; then
    cmp -s <(sort "$SELECTED") <(sort "$RUN") || fail "$RUN is not exactly the selected suites"
  else
    extra="$(grep -vxF -f "$SELECTED" "$RUN" || true)"
    [ -z "$extra" ] || fail "$RUN runs suites the selector did not select: $(echo "$extra" | head -3 | tr '\n' ' ')"
  fi
  exit 0
fi

LIST="${2:-}"; RDIR="${3:-}"
case "$MODE" in only|without) [ -n "$LIST" ] && [ -d "$RDIR" ] ;; *) false ;; esac || usage
ALL="$(mktemp)"; WANT="$(mktemp)"; OUT="$(mktemp)"; trap 'rm -f "$ALL" "$WANT" "$OUT"' EXIT
cat > "$ALL"
grep -vE '^(#|$)' "$LIST" > "$WANT" || true
dup="$(sort "$WANT" | uniq -d)"
[ -z "$dup" ] || { printf 'listed twice in %s:\n%s\n' "$LIST" "$dup" >&2; exit 3; }
stale="$(grep -vxF -f "$ALL" "$WANT" || true)"
[ -z "$stale" ] || { printf 'listed in %s but not discovered:\n%s\n' "$LIST" "$stale" >&2; exit 3; }
if [ "$MODE" = only ]; then
  # No match at all is an empty leg, which the caller must not run as a pass.
  grep -xF -f "$WANT" "$ALL" > "$OUT"
else
  grep -vxF -f "$WANT" "$ALL" > "$OUT" || true
fi
printf 'mode=%s\nlist_sha256=%s\noutput_sha256=%s\n' "$MODE" "$(sha "$LIST")" "$(sha "$OUT")" > "$RDIR/selector.$MODE.receipt"
cat "$OUT"
