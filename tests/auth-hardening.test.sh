#!/usr/bin/env bash
# Test for src/auth_hardening.sh::harden_auth_dir — the boot-time hardening of
# per-host secrets under state/auth/. Calls the REAL sourced function (not a
# re-implementation) so it tests what startup.sh actually runs.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../src/auth_hardening.sh
source "$REPO/src/auth_hardening.sh"

fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok  $1"; else echo "  FAIL $1 — want $3 got $2"; fails=$((fails+1)); fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/state/auth"
# Seed loose modes — the exact exposure found on 2026-07-28 (0755 dir, 0644 secrets).
chmod 755 "$TMP/state/auth"
printf '{}' > "$TMP/state/auth/cloud-auth.json";  chmod 644 "$TMP/state/auth/cloud-auth.json"
printf '{}' > "$TMP/state/auth/device.json";      chmod 644 "$TMP/state/auth/device.json"

harden_auth_dir "$TMP"

check "state/auth dir tightened to 0700" "$(stat -f '%Lp' "$TMP/state/auth")" "700"
check "cloud-auth.json tightened to 0600" "$(stat -f '%Lp' "$TMP/state/auth/cloud-auth.json")" "600"
check "device.json tightened to 0600"     "$(stat -f '%Lp' "$TMP/state/auth/device.json")" "600"

# Idempotent: a second run leaves them 0700/0600 (never widens).
harden_auth_dir "$TMP"
check "idempotent — dir stays 0700"  "$(stat -f '%Lp' "$TMP/state/auth")" "700"

# Fail-safe: a missing workspace / missing auth dir is a clean no-op (rc 0).
harden_auth_dir "$TMP/does-not-exist"; check "missing auth dir is a no-op (rc 0)" "$?" "0"
harden_auth_dir ""; check "empty workspace arg is a no-op (rc 0)" "$?" "0"

if [ "$fails" -eq 0 ]; then echo "PASS — auth-hardening"; else echo "FAIL — $fails"; exit 1; fi
