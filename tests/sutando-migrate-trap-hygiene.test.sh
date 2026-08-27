#!/usr/bin/env bash
# The cleanup trap must only ever remove paths THIS process created.
# Regression (review finding on #3418): the EXIT trap referenced
# _VERDICTS_TMP before the script initialized it, so an inherited
# environment value was rm -f'd on every exit — including `--help`.
set -u
cd "$(dirname "$0")/.."
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok  $1"; else echo "FAIL  $1 — got '$2', want '$3'"; fails=$((fails+1)); fi; }

victim="$(mktemp -t trap-hygiene-victim.XXXXXX)"
echo "precious" > "$victim"

# kewei's exact repro shape: inherited var + a non-mutating command.
_VERDICTS_TMP="$victim" bash scripts/sutando-migrate.sh --help >/dev/null 2>&1
rc=$?
check "--help exits 0 with an inherited _VERDICTS_TMP" "$rc" "0"
check "the inherited path SURVIVES (trap must not delete what it did not create)" \
  "$( [ -f "$victim" ] && echo alive || echo deleted )" "alive"
# non-JSON path variant of the same control (the finding asked for one).
victim2="$(mktemp -t trap-hygiene-victim2.XXXXXX).txt"
printf 'plain text\n' > "$victim2"
_VERDICTS_TMP="$victim2" bash scripts/sutando-migrate.sh --help >/dev/null 2>&1
check "a non-JSON inherited path survives too" \
  "$( [ -f "$victim2" ] && echo alive || echo deleted )" "alive"
rm -f "$victim" "$victim2"

if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL PASS"
