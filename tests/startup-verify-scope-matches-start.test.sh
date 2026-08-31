#!/usr/bin/env bash
# 7845 is verified only when startup ATTEMPTED it: verify must gate on the same
# PERM_OK condition as start, or a deliberate skip renders as a crash.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/src/startup.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "startup verify scope matches start:"

# Terminate on `for port_name`, present on BOTH revisions: anchoring on
# VERIFY_SETTLE_S ran to EOF on main and executed the verification loop.
BLOCK="$(awk '/^VERIFY_PORTS="\$WEB_CLIENT_PORT/,/^for port_name in/' "$SRC" \
         | sed '$d')"
if [ -z "$BLOCK" ]; then
  bad "VERIFY_PORTS assembly extracted" "anchors not found in src/startup.sh"
  echo "  Total: 1 — pass: 0, fail: 1"; exit 1
fi
ok "VERIFY_PORTS assembly extracted from src/startup.sh"

# Write the block to a file and SOURCE it. Passing it through `bash -c "...$BLOCK..."`
# crosses a quoting boundary the block's own quotes break, silently and with no stderr.
BLK="$(mktemp)"; trap 'rm -f "$BLK"' EXIT
printf '%s\n' "$BLOCK" > "$BLK"

ports_for() { # $1 = PERM_OK value, or "" for unset
  if [ -n "$1" ]; then export PERM_OK="$1"; else unset PERM_OK; fi
  WEB_CLIENT_PORT=3000 SKIP_VOICE=1 OBS_COLLECTOR_READY=0 BLK="$BLK" bash -c '
    phone_stack_enabled() { return 1; }
    . "$BLK"
    echo "$VERIFY_PORTS"'
}

got1="$(ports_for 1)"
case "$got1" in
  *7845:screen-capture*) ok "PERM_OK=1 verifies 7845" ;;
  *) bad "PERM_OK=1 verifies 7845" "got: $got1" ;;
esac

got0="$(ports_for 0)"
case "$got0" in
  *7845*) bad "PERM_OK=0 does NOT verify 7845" \
              "a deliberate skip would render as a crash — got: $got0" ;;
  *) ok "PERM_OK=0 does NOT verify 7845" ;;
esac

# Unset must behave as 0: the gate reads ${PERM_OK:-0}, and an unset variable is
# the state on any host where the permission probe never ran.
gotU="$(ports_for "")"
case "$gotU" in
  *7845*) bad "PERM_OK unset behaves as 0" "got: $gotU" ;;
  *) ok "PERM_OK unset behaves as 0" ;;
esac

# The other ports must survive both branches — a gate that drops everything
# would pass the two assertions above for the wrong reason.
for p in 7844:dashboard 7843:agent-api; do
  case "$got0" in
    *"$p"*) ok "PERM_OK=0 still verifies $p" ;;
    *) bad "PERM_OK=0 still verifies $p" "got: $got0" ;;
  esac
done

# START and VERIFY must gate on the same variable. If the start branch stops
# using PERM_OK, this scope rule is silently guarding nothing.
if grep -qE 'if \[ "\$PERM_OK" -eq 1 \]; then' "$SRC" \
   && grep -qE 'if \[ "\$\{PERM_OK:-0\}" = "1" \]; then' "$SRC"; then
  ok "start and verify both gate on PERM_OK"
else
  bad "start and verify both gate on PERM_OK" "one of the two conditions moved"
fi

total=7
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1
