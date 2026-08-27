#!/usr/bin/env bash
# The default gateway lane must not claim unaddressed proactives whose peek
# room sits on a named lane's homeserver suffix — its gateway 403s them and
# the failure policy parks deliverable work as undeliverable (#3427 adds the
# bridge-side fence, read from GATEWAY_FOREIGN_SUFFIXES). This test pins the
# LAUNCHER half: the tracked config derives that env for the default lane
# from the configured named instances, so a deploy cannot forget it.
#
# Run: bash tests/gateway-default-lane-foreign-suffixes.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "default-lane GATEWAY_FOREIGN_SUFFIXES derivation:"

# Each case runs the SHIPPED function in a clean subshell so ambient
# AG2_REMOTE_TOKEN_* / GATEWAY_FOREIGN_SUFFIXES on the invoking host cannot
# leak into the derivation under test.
derive() {
  env -i HOME="$HOME" PATH="$PATH" "$@" bash -c \
    "source '$REPO/src/startup-runtime.sh' >/dev/null 2>&1; derive_foreign_suffixes"
}

# --- no named instances → empty (fence stays inert) -------------------------
got="$(derive)"
if [ "$got" = "" ]; then
  ok "no named instances derives empty"
else
  bad "no named instances derives empty" "got '$got'"
fi

# --- one named instance → its conventional suffix ---------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":dev.ag2.space" ]; then
  ok "AG2_REMOTE_TOKEN_DEV derives :dev.ag2.space"
else
  bad "AG2_REMOTE_TOKEN_DEV derives :dev.ag2.space" "got '$got'"
fi

# --- two named instances → comma-joined, both present -----------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x AG2_REMOTE_TOKEN_STAGING=y)"
case "$got" in
  *":dev.ag2.space"*) dev_in=1 ;; *) dev_in=0 ;;
esac
case "$got" in
  *":staging.ag2.space"*) stg_in=1 ;; *) stg_in=0 ;;
esac
if [ "$dev_in" = 1 ] && [ "$stg_in" = 1 ] && [ "$(printf '%s' "$got" | tr -cd ',' | wc -c | tr -d ' ')" = 1 ]; then
  ok "two instances derive both suffixes, comma-joined"
else
  bad "two instances derive both suffixes, comma-joined" "got '$got'"
fi

# --- operator override wins verbatim ----------------------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x GATEWAY_FOREIGN_SUFFIXES=":custom.example")"
if [ "$got" = ":custom.example" ]; then
  ok "operator GATEWAY_FOREIGN_SUFFIXES wins verbatim over derivation"
else
  bad "operator GATEWAY_FOREIGN_SUFFIXES wins verbatim over derivation" "got '$got'"
fi

# --- wiring: the default-lane spawn actually passes the derived value -------
# The named-lane loop must NOT get it (instanced lanes fence by
# GATEWAY_ROOM_SUFFIX; the foreign list is the default lane's mirror).
spawn_lines="$(grep -n 'GATEWAY_FOREIGN_SUFFIXES="\$(derive_foreign_suffixes)"' "$REPO/src/startup-runtime.sh" | wc -l | tr -d ' ')"
if [ "$spawn_lines" = 1 ]; then
  ok "exactly one spawn site injects the derived value (default lane)"
else
  bad "exactly one spawn site injects the derived value (default lane)" "found $spawn_lines"
fi

# --- negative control: the harness can fail ---------------------------------
got="$(derive AG2_REMOTE_TOKEN_DEV=x)"
if [ "$got" = ":wrong.example" ]; then
  bad "negative control" "harness cannot fail — ':wrong.example' matched"
else
  ok "negative control: a wrong expectation would be caught"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "$fails FAILURE(S)"
  exit 1
fi
